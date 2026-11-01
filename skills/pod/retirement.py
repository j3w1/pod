"""The owner's historical retirement of delivered objectives whose coordinator is lost (R112, R113).

Retirement abandons the named record and releases every worktree it records: the paths the decision
names, after the checks below, and every other recorded worktree, as a closed objective's are. The objective is not
closed, continued, QUALIFIED or successful: its Governor journal, admissions, obligations and
evidence stay exactly as recorded, and the record accepts no later write. No coordinator is
revived, and no successor, agent session or worker is created. CE stays the resource authority.

A released worktree is either an existing worktree of this repository or one proven absent: gone from
this host's filesystem, from the repository's Git worktree registry and from Orca's complete worktree
and terminal inventories (R113). A missing directory never proves that work stopped; the coordinator,
Run, Dispatch and start checks prove that.

A record on an older version of a current schema family is read whole, through R146's documented fields and its
format's allow-list, and keeps its bytes: its retirement entry is stored once beside it in
`historical-retirement.json`, as an evidence closure is stored, bound to those bytes and to the decision's use
receipt, and the decision's publication record for that objective binds the exact entry. Only a certified entry
reads `retired`; its presence alone fences every write (R205).
"""

from __future__ import annotations

from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import unicodedata

from .errors import PodError
from .util import (CHANNEL_FIELDS, atomic_json, bounded_json, bounded_text, channel_fields, decision_channel,
                   digest, exact, owner_decision)

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
# Cleanup's conservative hold on locations a record never listed. Retirement ends it for every record (R113, R206);
# `none` stays readable only so that a decision made before R206 refuses as changed facts, not as malformed.
UNRECORDED = ("hold_ends", "none")
GOVERNOR_ROW = {"kind", "id", "action", "candidate", "outcome"}
ADMISSION_ROW = {"kind", "id", "role", "state", "dispatch", "reported"}
NATIVE = {"runtime", "owner", "runs", "dispatches", "exited_terminals"}
PRESERVED_OUTCOMES = ("pending", "UNKNOWN", "FAILED")
MAX_OBJECTIVES = 16
MAX_LISTED = 64
# The whole request names at most 64 paths to check, which bounds the native reads made while every requested
# objective lock is held. Recorded worktrees it does not name are released without those reads (R206).
MAX_PATHS = MAX_LISTED
MAX_DELIVERY = 16
ACTION = ("Read it with pod status --objective {}; the owner's historical retirement released its worktrees: "
          "it is not completed, and its record accepts no writes.")
# An older record's retirement, stored beside its unchanged bytes (R205).
SIDECAR = "historical-retirement.json"
SIDECAR_SCHEMA = "pod-historical-retirement/v1"
PUBLICATION_SCHEMA = "pod-historical-retirement-publication/v1"
# What the retirement adds to an older record's logical view; the record itself never carries these.
ADDED = (FIELD, "decision_order", "decision_channels")
# The entry names the decision's durable use receipt; it has no older shape to stay compatible with.
SIDECAR_FIELDS = {"schema", "objective", "repository", "record", "receipt", FIELD, "decision_order"}

# The one older format retirement reads whole: the shape the 0.6.0 cutover left (R205). Each key is READ and proven
# by detection, holds only the resource-free values listed (a tuple), or is DESCRIPTIVE text that never names a
# native resource. Any other key or value refuses `retirement_scope_unproved`, naming its field.
READ, DESCRIPTIVE = "read", "descriptive"
OLDER_FORMAT = {"context": "pod-context/v4", "checkpoint": "pod-checkpoint/v2", "admission": "pod-admission/v3"}
OLDER_CONTEXT = {"schema": READ, "owner": READ, "admissions": READ, "checkpoint": READ, "revision": DESCRIPTIVE,
                 "constraints": DESCRIPTIVE, "interventions": ({},), "source_rejections": ({},)}
OLDER_CHECKPOINT = {"schema": READ, "objective": READ, "worktree": READ, "native_refs": READ,
                    "assignments": ([],), "questions": ([],), "continuity": (None,),
                    "criteria": DESCRIPTIVE, "candidate": DESCRIPTIVE, "plan_revision": DESCRIPTIVE,
                    "policy_revision": DESCRIPTIVE, "bundle_digest": DESCRIPTIVE, "pod_version": DESCRIPTIVE,
                    "next_safe_action": DESCRIPTIVE, "verification_gaps": DESCRIPTIVE,
                    "remaining_gates": DESCRIPTIVE}
OLDER_ADMISSION = {"schema": READ, "admission_id": READ, "objective": READ, "owner": READ, "state": READ,
                   "run_id": READ, "task_id": READ, "request_uuid": READ, "native_binding": READ, "worktree": READ,
                   "recovery": READ, "runtime": DESCRIPTIVE, "request": DESCRIPTIVE, "route_decision": DESCRIPTIVE,
                   "effective_evidence": DESCRIPTIVE, "plan_revision": DESCRIPTIVE, "packet_id": DESCRIPTIVE,
                   "created_at": DESCRIPTIVE, "updated_at": DESCRIPTIVE, "error": (None,), "failures": ([],),
                   "reuse_of": (None,)}
OLDER_RECOVERY = {"checkpoint_binding": READ, "placement_binding": READ, "failed_stage": (None,),
                  "native_start_state": ("ready",), "receipt": ("recorded",), "residual_resources": ([], None)}
OLDER_CHECKPOINT_BINDING = {"worktree": READ, "candidate": DESCRIPTIVE, "criteria": DESCRIPTIVE,
                            "objective_source": DESCRIPTIVE, "plan_revision": DESCRIPTIVE,
                            "policy_revision": DESCRIPTIVE}
OLDER_WORKTREE = {"path": READ, "branch": DESCRIPTIVE, "repo_key": DESCRIPTIVE, "repository": DESCRIPTIVE}
OLDER_REF = {"runId": READ, "runtime": DESCRIPTIVE}
OLDER_BINDING = ("dispatchId", "runId", "taskId", "workerId", "worktreeId", "terminalHandle")


def _encodable(value: str) -> bool:
    """Whether text can be written as UTF-8, as every record and decision digest is; a lone surrogate, which JSON can
    carry, cannot."""
    try:
        value.encode()
    except UnicodeEncodeError:
        return False
    return True


def _text(value: object, limit: int) -> bool:
    return isinstance(value, str) and 0 < len(value) <= limit and _encodable(value)


def _texts(value: object, bound: int, limit: int) -> bool:
    return isinstance(value, list) and len(value) <= bound and all(_text(item, limit) for item in value)


def _path_text(value: object) -> bool:
    """A recorded worktree's text. A path to release must also name nothing a filesystem read cannot (`_requested`);
    a recorded one retirement keeps held needs no such read, so it stays echoable as recorded (R113)."""
    return (_text(value, 4096) and value.startswith("/") and os.path.normpath(value) == value
            and ".." not in Path(value).parts)


def _record_row(row: object, older: bool = False) -> bool:
    """One preserved record. An older record documents no admission role (R146), so its role reads null."""
    if not isinstance(row, dict):
        return False
    if row.get("kind") == "governor":
        return (set(row) == GOVERNOR_ROW and all(_text(row[key], 256) for key in ("id", "action", "candidate"))
                and row["outcome"] in PRESERVED_OUTCOMES)
    if row.get("kind") == "admission":
        return (set(row) == ADMISSION_ROW and all(_text(row[key], 256) for key in ("id", "state"))
                and (_text(row["role"], 256) or older and row["role"] is None)
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


def _facts_valid(value: dict, older: bool = False) -> bool:
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
            and isinstance(value["records"], list)
            and all(_record_row(row, older) for row in value["records"]))


def validate_entry(value: object, *, older: bool = False) -> None:
    """The stored shape of a retirement; anything else makes the record unsupported."""
    native = value.get("native") if isinstance(value, dict) else None
    modern = isinstance(value, dict) and "decision_binding" in value and "provenance" in value
    entry_fields = (ENTRY | {"provenance"}, LOCATED_ENTRY | {"provenance"}) if modern else (ENTRY, LOCATED_ENTRY)
    if (not isinstance(value, dict) or set(value) - CHANNEL_FIELDS not in entry_fields
            or not all(_text(value[key], 1024) for key in ("at", "decision", "instruction", "reason", "repository"))
            or not _texts(value["objectives"], MAX_OBJECTIVES, 256) or not value["objectives"]
            or not _text(value["owner"], 256) or not _runs(value["runs"])
            or not isinstance(value["released"], list) or not 0 < len(value["released"]) <= MAX_PATHS
            or not all(_path_text(path) for path in value["released"])
            or not isinstance(value["preserved"], list)
            or not all(_record_row(row, older) for row in value["preserved"])
            or not _texts(value["unfinished"], MAX_LISTED, 256)
            or not _texts(value["delivery"], MAX_DELIVERY, 512) or not value["delivery"]
            or not isinstance(native, dict) or set(native) != NATIVE
            or not all(_text(native[key], 256) for key in ("runtime", "owner"))
            or any(type(native[key]) is not int or native[key] < 0 for key in ("runs", "dispatches", "exited_terminals"))
            or "locations" in value and not _locations_valid(value["locations"], value["released"])):
        raise PodError("state_unsupported", "Objective historical retirement record is malformed")
    owner_decision(value, kind="retirement", stored=True)


def refuse_retired(state: dict | None) -> None:
    """The one refusal an objective under historical retirement gives every write, its coordinator's included."""
    entry = state.get(FIELD) if isinstance(state, dict) else None
    if isinstance(entry, dict):
        name = ((state.get("checkpoint") or {}).get("objective")) or "this objective"
        raise PodError("objective_retired",
                       f"The owner's historical retirement of this objective at {entry['at']} released its worktrees; "
                       "it is not completed, and its record accepts no writes",
                       {"retired_at": entry["at"], "next_action": ACTION.format(name)})


# --------------------------------------------------------------------------- an older record's retirement (R205)


def sidecar_presence(root: Path) -> str:
    """`present`, `absent`, or `unknown` when this host cannot inspect the objective's directory."""
    try:
        os.lstat(root / SIDECAR)
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "unknown"
    return "present"


def refuse_sidecar(root: Path) -> None:
    """The write fence of an older record under historical retirement: every write refuses, as R112 requires of a
    current one. A retirement Pod cannot rule out is never written past."""
    presence = sidecar_presence(root)
    if presence == "absent":
        return
    if presence == "unknown":
        raise PodError("unsafe_state", "Whether this objective is under historical retirement cannot be read; "
                       "nothing was written", {"next_action": "restore access to the objective's state directory, "
                                                              "then retry"})
    try:
        at = _sidecar(root)[FIELD]["at"]
    except PodError:
        at = None
    raise PodError("objective_retired",
                   "The owner's historical retirement of this objective" + (f" at {at}" if at else "")
                   + " released its worktrees; it is not completed, and its record accepts no writes",
                   {"retired_at": at, "next_action": "Read it with pod status --objective NAME; nothing remains to "
                                                     "record."})


def older_scope(raw: dict, objective: str) -> tuple[list[str], str | None]:
    """R205's whole-record read of an older record for retirement: every Run it names (every checkpoint reference,
    admission `run_id` and binding `runId`), or the first field it cannot prove. Every key at every level must be on
    the format's allow-list, every resource-free field must hold only its listed values, and identities must agree:
    an admission's id, objective and owner with the record's, its binding's Run and Task with its own, and every
    recorded placement with the checkpoint's worktree. A malformed non-null binding refuses in every state."""
    runs: set[str] = set()

    def bad(where: str) -> tuple[list[str], str]:
        return [], where

    def unlisted(value: object, fields: dict, where: str) -> str | None:
        if not isinstance(value, dict):
            return where
        for key, item in value.items():
            handling = fields.get(key)
            if handling is None or isinstance(handling, tuple) and not any(
                    type(item) is type(allowed) and item == allowed for allowed in handling):
                return f"{where}.{key}" if where else str(key)
        return None

    def place(value: object, where: str) -> str | None:
        if value is None:
            return None
        return unlisted(value, OLDER_WORKTREE, where) or (None if _path_text(value.get("path")) else where + ".path")

    found = unlisted(raw, OLDER_CONTEXT, "")
    if found is not None:
        return bad(found)
    checkpoint, admissions, owner = raw.get("checkpoint"), raw.get("admissions", {}), raw.get("owner")
    if raw.get("schema") != OLDER_FORMAT["context"]:
        return bad("schema")
    found = unlisted(checkpoint, OLDER_CHECKPOINT, "checkpoint")
    if found is not None:
        return bad(found)
    if checkpoint.get("schema") != OLDER_FORMAT["checkpoint"]:
        return bad("checkpoint.schema")
    if checkpoint.get("objective") != objective:
        return bad("checkpoint.objective")
    worktree = checkpoint.get("worktree")
    found = place(worktree, "checkpoint.worktree")
    if found is not None:
        return bad(found)
    refs = checkpoint.get("native_refs", [])
    if not isinstance(refs, list):
        return bad("checkpoint.native_refs")
    for index, ref in enumerate(refs):
        where = f"checkpoint.native_refs[{index}]"
        found = unlisted(ref, OLDER_REF, where)
        if found is not None or not _text(ref.get("runId"), 256):
            return bad(found or where + ".runId")
        runs.add(ref["runId"])
    if not isinstance(admissions, dict):
        return bad("admissions")
    for key, row in admissions.items():
        where = f"admissions.{str(key)[:12]}"
        found = unlisted(row, OLDER_ADMISSION, where)
        if found is not None:
            return bad(found)
        for name, expected in (("schema", OLDER_FORMAT["admission"]), ("admission_id", key),
                               ("objective", objective), ("owner", owner)):
            if row.get(name) != expected:
                return bad(f"{where}.{name}")
        if not _text(row.get("run_id"), 256):
            return bad(where + ".run_id")
        runs.add(row["run_id"])
        if row.get("task_id") is not None and not _text(row["task_id"], 1024):
            return bad(where + ".task_id")
        if row.get("worktree") is not None and not _text(row["worktree"], 4096):
            return bad(where + ".worktree")
        binding = row.get("native_binding")
        if binding is not None:
            if (not isinstance(binding, dict) or set(binding) - set(OLDER_BINDING)
                    or not set(OLDER_BINDING) - {"terminalHandle"} <= set(binding)
                    or not all(_text(item, 1024) for item in binding.values())):
                return bad(where + ".native_binding")
            if binding["runId"] != row["run_id"] or binding["taskId"] != row.get("task_id"):
                return bad(where + ".native_binding")
            runs.add(binding["runId"])
        elif row.get("state") == "bound":
            return bad(where + ".native_binding")
        recovery = row.get("recovery")
        if recovery is None:
            continue
        found = unlisted(recovery, OLDER_RECOVERY, where + ".recovery")
        if found is not None:
            return bad(found)
        if recovery.get("placement_binding") not in (None, worktree):
            return bad(where + ".recovery.placement_binding")
        snapshot = recovery.get("checkpoint_binding")
        if snapshot is not None:
            found = unlisted(snapshot, OLDER_CHECKPOINT_BINDING, where + ".recovery.checkpoint_binding")
            if found is not None or snapshot.get("worktree") not in (None, worktree):
                return bad(found or where + ".recovery.checkpoint_binding.worktree")
    return sorted(runs), None


def _sidecar(root: Path) -> dict:
    """The stored entry document, read bounded and without following a link; anything else raises."""
    from .evidence_closure import MAX_FILE
    try:
        fd = os.open(root / SIDECAR, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as file:
            if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                raise PodError("state_unsupported", "The historical retirement entry is not a regular file")
            data = file.read(MAX_FILE + 1)
    except OSError as exc:
        raise PodError("state_unsupported", "The historical retirement entry cannot be read") from exc
    try:
        value = json.loads(data.decode("utf-8")) if len(data) <= MAX_FILE else None
    except (ValueError, UnicodeError, RecursionError):
        value = None
    record = value.get("record") if isinstance(value, dict) else None
    if (not isinstance(value, dict) or not SIDECAR_FIELDS <= set(value) <= SIDECAR_FIELDS | {"decision_channels"}
            or value["schema"] != SIDECAR_SCHEMA or not _text(value["objective"], 256)
            or not _text(value["repository"], 1024) or not isinstance(record, dict)
            or set(record) != {"context_sha256"} or not _text(record["context_sha256"], 64)
            or not isinstance(value["receipt"], str) or re.fullmatch(r"[0-9a-f]{64}\.json", value["receipt"]) is None):
        raise PodError("state_unsupported", "The historical retirement entry is malformed")
    validate_entry(value[FIELD], older=True)
    from .relay import validate_order
    validate_order(value["decision_order"])
    if "decision_channels" in value and value["decision_channels"] not in (1, 2, 3, 4, 5):
        raise PodError("state_unsupported", "The historical retirement entry's decision channels are unsupported")
    return value


def _unverified(reason: str) -> PodError:
    return PodError("retirement_unverified", f"The historical retirement entry is not certified: {reason}",
                    {"next_action": "inspect the objective state; its record accepts no writes"})


def _publication(capability, objective: str, document: dict) -> tuple[Path, dict]:
    """The publication record of one objective's retirement under a decision: written beside the decision's use
    receipt, under the same lock as the entry, binding the original record bytes and the exact entry (R205)."""
    path = capability._receipt_path().parent / (
        digest(f"pod-historical-retirement:{capability.binding['request_id']}:{objective}") + ".json")
    return path, {"schema": PUBLICATION_SCHEMA, "request_id": capability.binding["request_id"],
                  "authorization": digest(capability.verdict or capability.record), "objective": objective,
                  "context_sha256": document["record"]["context_sha256"], "entry_sha256": digest(document)}


def _certify(project: Path, stored: dict, objective: str) -> None:
    """Whether a stored entry certifies a retirement (R205): it names this repository and objective within its
    decided scope, carries a complete stored decision of the retirement kind and category whose durable use receipt
    exists, and the publication record that decision wrote for this objective, beside that receipt and under the
    same lock, binds these exact record bytes and this exact entry. An objective only named in the request, or
    refused in it, has no such record. Under the policy revision that accepted it, that policy must still permit
    its actor. This is Pod's cooperative check of its own persisted evidence; it does not resist a writer who
    rewrites every store."""
    from . import config, policy
    from .github import repository_context
    from .relay import Decision, validate_decision_record
    entry = stored[FIELD]
    repository = repository_context(project)["repo_key"]
    if stored["repository"] != repository or entry["repository"] != repository:
        raise _unverified("it names another repository")
    if stored["objective"] != objective or objective not in entry["objectives"]:
        raise _unverified("its decided objectives do not name this objective")
    try:
        record = validate_decision_record(entry, kind="retirement")
    except PodError:
        raise _unverified("it carries no complete stored decision") from None
    binding = record["decision_binding"]
    resources = binding["resources"]
    if (resources.get("repository") != repository or resources.get("objectives") != digest(entry["objectives"])
            or resources.get("primary") not in entry["objectives"] or binding["categories"] != ["scope_changes"]):
        raise _unverified("its decision binds another scope")
    snapshot = config.load(project)
    try:
        capability = Decision._recorded(project, resources["primary"], entry, snapshot=snapshot)
        if capability._receipt_path().name != stored["receipt"]:
            raise _unverified("it names another use receipt")
        capability.check_receipt()
    except PodError as exc:
        if exc.code == "retirement_unverified":
            raise
        raise _unverified(f"its decision's use receipt does not certify it ({exc.code})") from None
    path, expected = _publication(capability, objective, stored)
    try:
        published = bounded_json(path, limit=1024)
    except (PodError, OSError, ValueError):
        published = None
    if published != expected:
        raise _unverified("no publication of its decision records this objective's exact retirement")
    if (snapshot["status"] in ("valid", "missing")
            and digest(snapshot["decision_policy"]) == binding["decision_policy_revision"]):
        actor = record["actor"]
        actor = {"kind": "person", "channel": actor["channel"]} if actor["kind"] == "person" else actor
        if not policy.permits(snapshot["decision_policy"], binding["categories"], actor):
            raise _unverified("the decision policy that accepted it does not permit its decider")


def older_state(root: Path, project: Path | None = None) -> tuple[dict, dict, bytes] | None:
    """An older record's logical view, its documented fields and its exact bytes; None for any other record.

    The view is the record as read, never validated as current or converted, plus the entry its retirement stored
    beside it, which must name these exact bytes. A record that already carries a field retirement adds, or whose
    documented fields are unreadable, refuses. With `project`, a stored entry must also certify the retirement
    (`_certify`); only the decision publication reads without it, while it writes that entry."""
    from .evidence_closure import _documented, _read_record, _record_class, _sha
    raw, data = _read_record(root)
    if _record_class(raw) != "older":
        return None
    if set(ADDED) & set(raw):
        raise PodError("state_unsupported", "The older record already carries a field historical retirement adds")
    try:
        doc = _documented(raw, current=False)
    except PodError as exc:
        raise PodError("state_unsupported", f"The older record's documented fields are unreadable ({exc.code})") \
            from exc
    view = deepcopy(raw)
    if sidecar_presence(root) != "absent":
        stored = _sidecar(root)
        if stored["record"]["context_sha256"] != _sha(data) or stored["objective"] != doc["objective"]:
            raise PodError("state_unsupported", "The historical retirement entry names another record")
        if project is not None:
            _certify(project, stored, doc["objective"])
        view.update({key: deepcopy(stored[key]) for key in ADDED if key in stored})
    return view, doc, data


def older_retirement(project: Path, root: Path) -> str:
    """How an entry stored beside a record reads for every affirmative use (phase, readiness, settlement, proof,
    sweep, listing): `absent`, `retired` only when certified beside an older record, else `unverified`. Its mere
    presence, in any state, still fences every write (`refuse_sidecar`)."""
    presence = sidecar_presence(root)
    if presence == "absent":
        return "absent"
    try:
        found = older_state(root, project)
    except PodError:
        return "unverified"
    return "retired" if found is not None and isinstance(found[0].get(FIELD), dict) else "unverified"


def read_state(path: Path) -> dict:
    """A record as retirement and its decision publication read it: an older record's logical view, any other
    through the ordinary validating reader."""
    from .ledger import _read, _record_exists
    if _record_exists(path):
        found = older_state(path.parent)
        if found is not None:
            return found[0]
    return _read(path)


def _write_older(path: Path, value: dict, data: bytes, repository: str) -> None:
    """An older record's single retirement write: its entry created once beside the record, whose bytes stay
    unchanged, under that record's lock and the same decision publication as a current record's. Under the same
    lock, and before the entry, the publication record binding this objective's exact entry is written beside the
    decision's use receipt; it is what certifies the entry (`_certify`)."""
    from .evidence_closure import _create
    from .ledger import _lock
    from .relay import current_decision, publish_decision, stamp_decision
    from .util import protect_decision_record
    stamp_decision(value)
    protect_decision_record(value)
    validate_entry(value[FIELD], older=True)
    document = _older_document(value, data, repository)
    record, published = _publication(current_decision(), document["objective"], document)

    def publish() -> None:
        with _lock(record):
            if record.exists() or record.is_symlink():
                if bounded_json(record, limit=1024) != published:
                    raise PodError("objective_retired", "Another publication of this objective's retirement is "
                                   "recorded; nothing was written")
            else:
                atomic_json(record, published, limit=1024)
        if not _create(path.parent, document, name=SIDECAR):
            raise PodError("objective_retired", "A historical retirement of this objective is already recorded; "
                           "nothing was written")

    publish_decision(path, value, publish, read=read_state)


def older_status(project: Path, objective: str, root: Path) -> dict | None:
    """What status shows for an older record under historical retirement (R205): read-only, from the record's
    documented fields and its certified stored entry, as a current record under historical retirement reads. An
    entry that is malformed, stale or not certified reads blocked with readiness unavailable, never `retired`. None
    when no entry is stored or the record is not an older one; the ordinary reader then answers."""
    from .evidence_closure import _record_class, _read_record
    from .readiness import CONTRACT, unavailable
    if sidecar_presence(root) == "absent":
        return None
    try:
        raw, _ = _read_record(root)
    except PodError:
        raw = None
    if not isinstance(raw, dict) or _record_class(raw) != "older":
        return None
    out = {"objective": objective, "record_class": "older", "schema": "superseded"}
    try:
        view, doc, _ = older_state(root, project)
    except PodError as exc:
        return {**out, "status": "blocked", "blocker": exc.code, "retirement": None,
                "next_safe_action": "inspect the objective state; its record accepts no writes",
                "readiness": unavailable(exc.code, "the historical retirement entry beside the record cannot be "
                                         f"read or certified ({exc.code})")}
    entry, block = view[FIELD], status_block(view)
    settled, preserved = [], []
    for key, row in sorted(doc["admissions"].items()):
        item = {"admission": key, "role": row["role"], "dispatch": (row["native_binding"] or {}).get("dispatchId"),
                "state": row["state"]}
        if row["state"] == "bound":
            settled.append({**item, "settlement": "retirement", "retired_at": entry["at"]})
        elif row["state"] not in ("closed", "deferred"):
            preserved.append({**item, "outcome": "unknown", "retired_at": entry["at"]})
        else:
            settled.append(item)
    checkpoint = view["checkpoint"]
    action = ACTION.format(objective)
    runs = doc["runs"]
    return {**out, "status": "observed", "phase": "retired", "blocker": "objective_retired",
            "source": checkpoint.get("objective_source", "direct_objective"),
            "selected_worktree": checkpoint.get("worktree"),
            "documented": {"owner": doc["owner"], "runs": runs, "worktree": doc["worktree"], "issue": doc["issue"]},
            "checkpoint_join": {"native_refs": doc["native_refs"], "assignments": [],
                                "plan_revision": checkpoint.get("plan_revision"),
                                "candidate": checkpoint.get("candidate")},
            "assignments": {"active": [], "unverified": [], "settled": settled[:MAX_LISTED],
                            "preserved": preserved[:MAX_LISTED]},
            "retained_terminals": [], "worker_tabs": [], "native": None, "retirement": block,
            "readiness": {"contract": CONTRACT, "state": "retired", "acts_next": "none", "coordinator_needed": False,
                          "coordinator": {"handle": doc["owner"], "run": runs[0] if len(runs) == 1 else None,
                                          "runs": runs[:32], "generation": None, "runtime": None,
                                          "liveness": "not_read", "code": None},
                          "finalization": None, "reason": {"retired_at": entry["at"], "reason": entry["reason"]},
                          "summary": "Ended by the owner's historical retirement: not completed or closed, and its "
                                     "record accepts no writes."},
            "next_safe_action": action, "next_action": action}


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


def _reservations(state: dict, release: list[str]) -> dict:
    """What retirement holds and ends, computed from the whole record on every read (R206): an objective under historical retirement holds
    no worktree, so `retained` is empty, `also_released` lists the recorded worktrees the decision did not name, and
    cleanup's hold on locations the record never listed ends. Nothing is stored, so a retirement recorded before
    this rule, current entry or older record's sidecar alike, reads the same way."""
    recorded, _ = _recorded(state)
    return {"retained": [], "also_released": sorted(recorded - set(release)), "unrecorded_locations": "hold_ends"}


def status_block(state: dict) -> dict | None:
    """Public meaning of a retirement for status and the objective list; never closure or success."""
    entry = state.get(FIELD) if isinstance(state, dict) else None
    if not isinstance(entry, dict):
        return None
    held = _reservations(state, entry["released"])
    return {"retired_at": entry["at"], "completed": False, "released": list(entry["released"]),
            "locations": [dict(row) for row in entry["locations"]] if "locations" in entry else None, **held,
            "preserved": [dict(row) for row in entry["preserved"]], "counts": counts(entry["preserved"]),
            "unfinished": list(entry["unfinished"]), "delivery": list(entry["delivery"]),
            "reason": entry["reason"], "decided_with": list(entry["objectives"]),
            **channel_fields(decision_channel(entry))}


def _governor_rows(project: Path, objective: str) -> list[dict]:
    from .governor import _read_journal, _record_path
    journal = _read_journal(_record_path(project, objective))
    return [{"kind": "governor", "id": row["record_id"], "action": row["action"]["kind"],
             "candidate": row["action"]["candidate"], "outcome": row["outcome"]}
            for row in journal["actions"]
            if row["decision"] == "ALLOW" and row["outcome"] in PRESERVED_OUTCOMES]


def _claimants(project: Path, objective: str) -> tuple[list[str], list[str]]:
    """Objectives of this repository whose claim names this one as successor, and the records Pod cannot read, by
    path, for callers to name. Every readable record is read whatever the others are, so an unreadable record never
    hides a readable claim, and it holds only its own resources (R206). Directories without a record are skipped."""
    from .github import repository_context
    from .ledger import CONTEXT_LIMIT, _record_exists, record_name, state_root
    from .listing import _name
    from .history import load_context
    context = repository_context(project)
    root = state_root(project)
    try:
        if root.is_symlink():
            return [], [str(root)]
        with os.scandir(root) as frontier:
            entries = sorted(frontier, key=lambda entry: entry.name)
    except FileNotFoundError:
        return [], []
    except OSError:
        return [], [str(root)]
    found, unreadable = [], []
    for entry in entries:
        if entry.name.startswith("."):
            continue
        try:
            if entry.is_symlink():
                unreadable.append(entry.path)
                continue
            if not entry.is_dir(follow_symlinks=False) or not _record_exists(Path(entry.path) / "context.json"):
                continue
            raw = load_context(Path(entry.path) / "context.json", validate=False)
        except (OSError, PodError):
            unreadable.append(entry.path)
            continue
        if not isinstance(raw, dict):
            unreadable.append(entry.path)
            continue
        link = raw.get("continued_by")
        name = _name(raw)
        if (isinstance(link, dict) and link.get("objective") == objective and name is not None
                and record_name(project, context, name) == entry.name):
            found.append(name)
    return found, unreadable


def _readable(place: object) -> bool:
    """Whether a location is an absolute path this host can resolve; any other makes its inventory unusable."""
    if not isinstance(place, str) or not place.startswith("/"):
        return False
    try:
        os.fsencode(place)
    except (UnicodeError, ValueError):
        return False
    return "\x00" not in place


def _folded(name: str) -> str:
    """A name as a case-insensitive directory may match it: canonical caseless form."""
    return unicodedata.normalize("NFD", unicodedata.normalize("NFD", name).casefold())


def _standing(place: str) -> list[tuple[tuple[int, int], tuple[str, ...]]]:
    """Each directory this host shows on the way to a location, as its device and inode, with the folded names
    below it. Links are followed, so a link, a bind mount or a case-insensitive directory reads as the directory it
    is; the walk ends where nothing more can be read, missing or not."""
    parts = Path(place).parts
    found = []
    for depth in range(1, len(parts) + 1):
        try:
            shown = os.stat(Path(*parts[:depth]))
        except OSError:
            break
        if not stat.S_ISDIR(shown.st_mode):
            break
        found.append(((shown.st_dev, shown.st_ino), tuple(_folded(part) for part in parts[depth:])))
    return found


def _placed(path: str) -> tuple[tuple[int, int], tuple[str, ...]] | None:
    """The path's deepest existing directory and the folded names below it; None when it cannot be placed."""
    try:
        return _standing(path)[-1]
    except (OSError, ValueError, IndexError):
        return None


def _walk(candidate: str) -> list[tuple[tuple[int, int], tuple[str, ...]]] | None:
    """Every directory a location's walk passes, as spelled and with its links resolved; None when it cannot be
    walked."""
    try:
        return [step for place in dict.fromkeys((candidate, os.path.realpath(candidate))) for step in _standing(place)]
    except (OSError, ValueError):
        return None


def _within(candidate: object, path: str) -> bool:
    """Whether a location the registry or another inventory names is the path or lies beneath it on this host. The
    path is placed at its deepest existing directory; the location matches when its own walk, as spelled or with its
    links resolved, passes through that directory, by device and inode, with the same folded names below it. So a
    link, even one left dangling by the path's removal, a bind mount or a case-insensitive directory cannot hide a
    match; over-matching only leaves the path unknown. Inventories admit only readable locations; should one still
    not resolve, it could be anywhere and counts as a match (R113)."""
    return _passes(candidate, _placed(path), _walk)


def _passes(candidate: object, placed: tuple | None, walk) -> bool:
    """`_within` for a path already placed, taking the location's walk from `walk`: one sweep places the path once,
    and one objective's host walks each location once (`_Host.walk`)."""
    if not isinstance(candidate, str) or not candidate.startswith("/"):
        return False
    if placed is None:
        return True
    steps = walk(candidate)
    if steps is None:
        return True
    anchor, below = placed
    return any(key == anchor and names[:len(below)] == below for key, names in steps)


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
    if (isinstance(rows, list) and result.get("truncated") is False and type(result.get("totalCount")) is int
            and result["totalCount"] == len(rows)
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
    most once for one objective, as is each location they name walked. A failed, partial, host-incomplete or
    unlocatable read stays unavailable, never an empty list."""

    def __init__(self, project: Path):
        self.project, self._reads, self._walks = project, {}, {}

    def walk(self, place: str) -> list | None:
        """A location's walk, taken once for this objective: every path it is compared with is placed afresh, and
        the filesystem checks before and after the reads (`_filesystem`, `_moved`) never use it."""
        if place not in self._walks:
            self._walks[place] = _walk(place)
        return self._walks[place]

    def registered(self, path: str) -> bool | None:
        """Whether the registry names the path or anything beneath it; None while the registry is unusable."""
        found = self.registry()
        if found is None:
            return None
        placed = _placed(path)
        return any(_passes(item, placed, self.walk) for item in found)

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
            from .orca import complete_list, read_command
            self._reads[kind] = None
            try:
                result = complete_list(kind.removesuffix("s"), read_command)["result"]
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
        placed = _placed(path)
        return [row for row, places in zip(found["rows"], found["places"])
                if any(_passes(place, placed, self.walk) for place in places)]


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
    registered = host.registered(path)
    if registered is None:
        return _location(path, "unknown", "path_unverified", "Git's worktree registry of this repository could not "
                         "be read completely.", filesystem="absent")
    if registered:
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
             "inaccessible": "This host cannot read the path or one of its parent directories.",
             "noncanonical": "The recorded spelling is not this host's canonical form of the path (such as a `//` "
                             "root), so Git's and Orca's entries cannot be matched to it exactly."}


def _locate(path: str, repository: str, host: _Host, entries: set[str]) -> dict:
    """One released path: existing, proven absent, foreign, or unknown with the reason absence stays unproven."""
    shown = _filesystem(path)
    if shown in ("directory", "absent") and os.path.realpath(path) != path:
        # Inventories are compared after resolving links, so only a canonical spelling can match them (R113).
        shown = "noncanonical"
    if shown == "directory":
        return _existing(path, repository, entries, host)
    if shown == "absent":
        return _absent(path, host)
    return _location(path, "unknown", "path_unverified", _UNPROVEN[shown], filesystem=shown)


def fleet_dispatches(fleet: dict) -> tuple[list[str], tuple[str, str] | None]:
    """One Run's worker list as R112 reads it: complete, naming a runtime and Dispatch identities Pod can record and
    pass back to Orca exactly. Returns those Dispatch identities, or the refusal that holds the proof."""
    from .orca import _passable
    if fleet.get("complete") is not True:
        return [], ("workers_unverified", "A Run's worker list is incomplete.")
    # An identity Pod would record or pass back to Orca must be text it can hold exactly (R113).
    if not _passable(fleet["runtime"]) or not _text(fleet["runtime"], 256):
        return [], ("workers_unverified", "A Run's worker list names an Orca runtime identity that is not UTF-8 "
                    "text without NUL of at most 256 characters, so Pod cannot record it.")
    found = []
    for row in fleet["workers"]:
        dispatch = row.get("dispatchId") if isinstance(row, dict) else None
        if not isinstance(dispatch, str) or not dispatch:
            return [], ("workers_unverified", "A worker has no Dispatch identity.")
        if not _passable(dispatch):
            return [], ("workers_unverified", "A worker's Dispatch identity cannot be encoded as UTF-8 text "
                        "without NUL, so Pod cannot read it.")
        found.append(dispatch)
    return found, None


def work_activity(admissions: dict, dispatches: dict, *, port) -> tuple[tuple | None, int]:
    """R112's proof that no recorded work is live, read-only: each reserved or unresolved start Orca may still hold,
    then every Dispatch of the Runs' complete worker lists and of each bound admission, with its exact recorded
    identity. `dispatches` names the worker Dispatches (value None); bound admissions are added to it. Returns the
    first unproven fact as (condition, message, next action), or None, and how many Dispatches read settled."""
    from .ledger import _native_assignment_claim_free as _native_assignment_settled
    from .operations import _receipt_from_request_show
    activity, settled = None, 0
    for key, row in admissions.items():
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
    return activity, settled


def _detect(project: Path, objective: str, release: list[str], state: dict | None, *, port,
            repository: str, host: _Host, doc: dict | None = None) -> dict:
    """Fresh facts for one objective, read-only; an unproven fact refuses it alone. An older record (`doc`, its
    documented fields) takes its Runs and admissions from those fields alone (R205)."""
    from .handoff import unfinished
    from .ledger import _run_references, map_of
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
        held = _reservations(state, entry["released"])
        verdict.update(status="retired", condition="objective_retired", facts={
            "objective": objective, "owner": entry["owner"], "runs": list(entry["runs"]),
            "release": list(entry["released"]),
            "locations": [dict(row) for row in entry["locations"]] if "locations" in entry else None,
            "retained": held["retained"], "unrecorded_locations": held["unrecorded_locations"],
            "records": list(entry["preserved"])}, also_released=held["also_released"],
            message="The owner's historical retirement already covers this objective.", next_action=ACTION.format(objective))
        return verdict
    map_state = map_of(state)
    if map_state is not None and map_state.get("closure"):
        return fail("objective_closed", "The objective is closed; a closed objective needs no retirement.")
    from .evidence_closure import present
    from .ledger import objective_root
    if present(objective_root(project, objective)):
        return fail("objective_closed", "The objective was closed by evidence, coordinator gone; a closed objective "
                    "needs no retirement.")
    claimants, unreadable = _claimants(project, objective)
    if claimants:
        # An interrupted claim recorded the predecessor's link before this objective's own back-link.
        return fail("objective_continues", f"Objective {claimants[0]} records this objective as its successor; "
                    "historical retirement never ends a successor that holds a claim.")
    if unreadable:
        # An unreadable record holds only its own resources (R206); it is named, and detection goes on.
        verdict["unavailable_records"] = unreadable[:MAX_LISTED]
    named = None
    if doc is not None:
        # An older record is read whole: a field detection does not prove refuses before any native read (R205).
        named, field = older_scope(state, objective)
        if field is not None:
            verdict["field"] = field
            return fail("retirement_scope_unproved", f"The superseded record's field {field} is not one "
                        "historical retirement reads and proves for this format, or contradicts the record's "
                        "identities.",
                        "Retirement cannot prove this record's scope; the project lead resolves it.")
    owner = state.get("owner")
    if not owner:
        return fail("owner_unrecorded", "The objective records no coordinator.")
    verdict["facts"]["owner"] = owner
    try:
        if unfinished(state) is not None:
            return fail("handoff_pending", "The objective has an unfinished owner handoff.")
        from .window import unfinished as window_unfinished
        if (window_unfinished(state) or {}).get("state") == "pending":
            return fail("handoff_pending", "The objective has a pending window continuation.")
        refs = sorted(set(doc["runs"]) | set(named)) if doc is not None else sorted(_run_references(state))
        recorded, _ = _recorded(state)
        records = _governor_rows(project, objective)
    except PodError as exc:
        return fail("scope_unreadable", f"The objective's record or Governor journal is unreadable ({exc.code}).")
    except (KeyError, TypeError, AttributeError):
        return fail("scope_unreadable", "The objective's record is malformed.")
    for path in release:
        if path not in recorded:
            return fail("path_not_recorded", "A path to release is not one of the objective's recorded worktrees.",
                        "Name only worktrees pod status --list records for this objective.")
    held = _reservations(state, release)
    verdict["facts"].update(retained=held["retained"], unrecorded_locations=held["unrecorded_locations"])
    # Computed, never decided: the decision binds the paths it names, and the rest of the record follows (R206).
    verdict["also_released"] = held["also_released"]
    if not refs:
        return fail("scope_unreadable", "The objective records no Run whose work could be proven settled.")
    admissions = doc["admissions"] if doc is not None else state["admissions"]
    for key in sorted(admissions):
        row = admissions[key]
        if row["state"] in ("closed", "deferred"):
            continue
        records.append({"kind": "admission", "id": key, "role": row["role"], "state": row["state"],
                        "dispatch": (row.get("native_binding") or {}).get("dispatchId"),
                        # An older record's report is never read: its bound start counts as unreported (R146).
                        "reported": row["reported"] if doc is not None else row["state"] == "bound"
                        and (row.get("report") or {}).get("status") == "validated_observation"})
    # The preserved table has no count bound; the record byte caps bound it (R206).
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
        changed = binding.get("run") == run and binding.get("generation") != shown["consumer_generation"]
        explicitly_unbound = shown.get("coordinator_known") is True and shown["coordinator_handle"] is None
        if changed and not explicitly_unbound:
            return fail("generation_changed", "A recorded Run's consumer generation changed.")
        found, refused = fleet_dispatches(fleet)
        if refused is not None:
            return fail(*refused)
        runtime = runtime or fleet["runtime"]
        runs.append({"run": run, "generation": shown["consumer_generation"]})
        for dispatch in found:
            dispatches[dispatch] = None
    verdict["facts"]["runs"] = runs
    # Every remaining fact is read before any refusal, so a refusal from here on keeps complete facts.
    activity, settled = work_activity(admissions, dispatches, port=port)
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
    if doc is not None:
        verdict["record_class"] = "older"
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
        if not _encodable(row["objective"]):
            raise PodError("invalid_request", "objective must be text that can be encoded as UTF-8")
        if (not isinstance(row["release"], list) or not 0 < len(row["release"]) <= MAX_PATHS
                or not all(_path_text(path) and "\x00" not in path for path in row["release"])
                or len(set(row["release"])) != len(row["release"])):
            raise PodError("invalid_request", f"release names 1 to {MAX_PATHS} distinct absolute normalized paths")
        rows.append({"objective": row["objective"], "release": list(row["release"])})
    if len({row["objective"] for row in rows}) != len(rows):
        raise PodError("invalid_request", "Each objective is named once")
    if sum(len(row["release"]) for row in rows) > MAX_PATHS:
        raise PodError("invalid_request", f"A request releases at most {MAX_PATHS} paths across its objectives")
    return rows


def _decision(value: object, requested: list[dict], repository: str) -> dict:
    decision = exact(value, DECISION | CHANNEL_FIELDS, DECISION, name="retirement_decision")
    bounded_text(decision["instruction"], name="instruction", limit=1024)
    bounded_text(decision["reason"], name="reason", limit=1024)
    try:
        digest(decision)
    except (UnicodeEncodeError, TypeError, ValueError):
        raise PodError("invalid_request", "The decision must be JSON text that can be encoded as UTF-8; nothing was "
                       "written") from None
    mismatch = PodError("retirement_decision_mismatch", "Only the owner's direct instruction for exactly the "
                        "requested objectives and paths permits retirement. Nothing was written.",
                        {"next_action": "detect again without a decision and confirm its facts with the owner"})
    from .relay import decision_input, _raw_decision
    generic = decision_input(decision)
    if (decision["provenance"] != "user_direct" and not generic) or decision["repository"] != repository:
        raise mismatch
    blocks = decision["objectives"]
    if not isinstance(blocks, list) or len(blocks) != len(requested) or any(
            not isinstance(block, dict) or block.get("objective") != row["objective"]
            or block.get("release") != row["release"] for block, row in zip(blocks, requested)):
        raise mismatch
    if generic:
        _raw_decision(decision)
        return dict(decision)
    return owner_decision(decision, kind="retirement")


def _retirement_entry(decision, requested, repository, facts, verdict, block, at):
    from .relay import current_decision
    capability = current_decision()
    return {"at": at, "decision": digest(decision), "instruction": decision["instruction"], "reason": decision["reason"],
            **channel_fields(decision), **({"provenance": decision["provenance"]} if capability is not None else {}),
            "repository": repository, "objectives": [item["objective"] for item in requested],
            "owner": facts["owner"], "runs": facts["runs"], "released": facts["release"], "locations": facts["locations"],
            "preserved": facts["records"], "unfinished": verdict["unfinished"], "delivery": list(block["delivery"]), "native": verdict["native"]}


def _block_valid(block: dict, older: bool = False) -> bool:
    """One objective's part of the decision; a malformed one refuses only that objective."""
    return (set(block) == DECIDED and _facts_valid(block, older) and _texts(block["delivery"], MAX_DELIVERY, 512)
            and bool(block["delivery"]))


def _released(locations: list[dict]) -> str:
    return ", ".join(row["path"] + (" (absent)" if row["disposition"] == "absent" else " (existing worktree)")
                     for row in locations)


def _effects(row: dict) -> str:
    """What else retirement releases for one objective (R113, R206): an objective under historical retirement holds no worktree."""
    also = row.get("also_released") or []
    said = f" Retirement releases every worktree objective {row['objective']} records"
    if also:
        said += ", including the ones not named above: " + ", ".join(also)
    return said + ("; Pod's record holds nothing afterwards, including any location the record never listed, and "
                   "CE's own cleanup protections still apply.")


def _question(eligible: list[dict], refused: list[dict], tally: dict) -> str:
    """One plain question naming every objective a decision on this request could bind, its paths and how each
    stands, and what cleanup holds for it afterwards (R113)."""
    names = [row["objective"] for row in eligible]
    standing = {row["path"]: row for item in eligible for row in item["facts"]["locations"]}
    question = (f"Optional abandonment: may Pod retire {'objective' if len(names) == 1 else 'objectives'} {', '.join(names)} "
                f"without claiming completion, releasing {_released([standing[key] for key in sorted(standing)])} "
                "for cleanup? This is an authority decision to abandon the record, never delivery proof.")
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
    if any(item["disposition"] == "existing" for row in eligible + later for item in row["facts"]["locations"]):
        question += (" An existing worktree is one of this repository that stands at the recorded path now; Pod "
                     "cannot prove it is the one the work used.")
    question += "".join(_effects(row) for row in eligible)
    older = [row["objective"] for row in eligible if row.get("record_class") == "older"]
    if older:
        question += (f" {', '.join(older)} {'uses' if len(older) == 1 else 'use'} a superseded record schema: the "
                     "record is read through its documented fields and stays byte-identical; the retirement is "
                     "stored beside it.")
    undelivered = [row["objective"] for row in eligible if any(
        item.get("code") == "delivery_unrecorded" for item in (row.get("completion") or {}).get("conditions", []))]
    if undelivered:
        question += (f" No delivery is recorded for {', '.join(undelivered)}: a decision states that plainly in "
                     "its delivery references (such as \"none recorded\") and never names a delivery.")
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


DELIVERED_ACTION = "pod sweep closes it by evidence; no owner decision is needed"
# Refusals read from Pod's records alone, before any native read; a delivery proof never replaces them.
_RECORD_REFUSALS = ("objective_unknown", "objective_continued", "objective_continues", "objective_closed",
                    "scope_unreadable", "owner_unrecorded", "handoff_pending", "path_not_recorded",
                    "retirement_scope_unproved")


def _proof_requested(value: object) -> list[str]:
    """The objectives a delivery proof closes: each named once, with an optional release list that evidence closure
    does not need, since a closed objective's recorded worktrees are all freed (R147)."""
    if not isinstance(value, list) or not 0 < len(value) <= MAX_OBJECTIVES:
        raise PodError("invalid_request", f"objectives is a list of 1 to {MAX_OBJECTIVES} entries")
    names = []
    for item in value:
        row = exact(item, REQUEST, {"objective"}, name="retirement_objective")
        bounded_text(row["objective"], name="objective", limit=256)
        if not _encodable(row["objective"]):
            raise PodError("invalid_request", "objective must be text that can be encoded as UTF-8")
        if "release" in row:
            _requested([row])
        names.append(row["objective"])
    if len(set(names)) != len(names):
        raise PodError("invalid_request", "Each objective is named once")
    return names


def _close_proved(project: Path, objectives: object, *, port=None) -> dict:
    """`proof: delivery` in place of a decision (R147): each proved objective closes by evidence, each other is
    refused with its conditions, a task for the project lead. Nothing asks the owner."""
    from .evidence_closure import LEAD_ACTION, close_by_evidence
    from .github import repository_context
    from .ledger import record_name
    names = _proof_requested(objectives)
    context = repository_context(project, fresh=True)
    if context["repo_key"] is None:
        raise PodError("retirement_scope_unavailable", "Historical retirement needs a Git repository",
                       {"next_action": "run it from the repository's canonical checkout"})
    results = []
    for name in names:
        verdict = close_by_evidence(project, name, caller="historical-retirement", port=port)
        status = {"closed": "closed", "replayed": "replayed",
                  "chain_completed": "chain_completed"}.get(verdict["status"], "refused")
        row = {"objective": name, "record": record_name(project, context, name), "status": status,
               "proof": verdict["status"], "conditions": verdict.get("conditions", [])}
        if status == "refused":
            row.update(condition=verdict["conditions"][0]["code"] if verdict.get("conditions") else verdict["status"],
                       party="lead", next_action=LEAD_ACTION)
        else:
            # `predecessors` names each predecessor entry this call wrote (R146); a replay writes none.
            row.update(closure=verdict.get("closure"), predecessors=verdict.get("predecessors", []),
                       next_action=f"Read it with pod status --objective {name}; it is closed by evidence.")
            row.update({key: verdict[key] for key in ("source_issues", "parents_open") if key in verdict})
        results.append(row)
    done = [row for row in results if row["status"] != "refused"]
    return {"status": "closed" if len(done) == len(results) else "partially_closed" if done else "refused",
            "repository": context["repo_key"], "proof": "delivery", "objectives": results}


def _delivered(project: Path, verdict: dict, *, port) -> dict:
    """Detection's delivered objective leaves the owner question: delivery proved closes it by evidence (R147). Its
    retirement facts stay complete, so an optional owner decision still binds them exactly as before."""
    from .evidence_closure import prove
    proof = prove(project, verdict["objective"], port=port, readback=False)
    if proof["status"] != "proved":
        from .evidence_closure import LEAD_ACTION
        return {**verdict, "completion": {"status": proof["status"], "conditions": proof["conditions"]},
                "party": "lead", "next_action": LEAD_ACTION,
                "abandonment": "R112 retirement remains an optional abandonment decision; it never proves delivery."}
    return {**verdict, "status": "delivered", "condition": "delivered", "retirement": verdict["condition"],
            "delivery": [row["pr"] for row in proof["facts"]["delivery"]],
            "message": "Delivery is proved; this objective closes by evidence, with no owner decision.",
            "next_action": DELIVERED_ACTION}


def _load_record(path: Path, project: Path) -> tuple[dict, dict | None, bytes | None]:
    """A record for retirement: an older one as its logical view with its documented fields and exact bytes, its
    stored entry certified, any other through the ordinary validating reader, which still refuses an unknown or
    malformed record."""
    from .ledger import _read
    found = older_state(path.parent, project)
    return found if found is not None else (_read(path), None, None)


def _older(path: Path) -> bool:
    from .evidence_closure import _read_record, _record_class
    try:
        return _record_class(_read_record(path.parent)[0]) == "older"
    except PodError:
        return False


def _older_document(value: dict, data: bytes, repository: str) -> dict:
    """The stored entry of an older record: what retirement adds to its view, bound to its exact bytes and naming
    the use receipt its decision's publication writes (R205)."""
    from .evidence_closure import MAX_FILE, _sha
    from .relay import current_decision
    from .util import encoded
    capability = current_decision()
    if capability is None or not isinstance(value.get(FIELD), dict) or "decision_order" not in value:
        raise PodError("decision_capability_required", "An older record's retirement needs its exact decision "
                       "capability; nothing was written")
    added = {key: value[key] for key in ADDED if key in value}
    document = {"schema": SIDECAR_SCHEMA, "objective": value["checkpoint"]["objective"], "repository": repository,
                "record": {"context_sha256": _sha(data)}, "receipt": capability._receipt_path().name, **added}
    if len(encoded(document, compact=True)) > MAX_FILE:
        raise PodError("record_too_large", "The historical retirement entry exceeds its bound; nothing was written")
    return document


def historical_retirement(project: Path, objectives: object, *, decision: object | None = None,
                          port=None, proof: object | None = None) -> dict:
    """Detect without a decision; retire with an exact one. Each objective succeeds or refuses alone.

    Detection names a delivered objective `delivered`, outside the owner question; `proof: delivery` closes each
    proved objective by evidence instead of retiring it (R147). An owner decision keeps working exactly as before.
    """
    from .github import repository_context
    from .ledger import _lock, _path, _write_retirement, record_name
    from .operations import OrcaPort
    if proof is not None:
        if decision is not None or proof != "delivery":
            raise PodError("invalid_request", "proof is \"delivery\", given in place of a decision")
        return _close_proved(project, objectives, port=port)
    requested = _requested(objectives)
    context = repository_context(project, fresh=True)
    repository = context["repo_key"]
    if repository is None:
        raise PodError("retirement_scope_unavailable", "Historical retirement needs a Git repository",
                       {"next_action": "run it from the repository's canonical checkout"})
    if decision is not None:
        decision = _decision(decision, requested, repository)
        legacy_digest = (digest({key: value for key, value in decision.items() if key != "channel"})
                         if decision["channel"] == "session" and "relay" not in decision else None)
    port = port or OrcaPort(project)
    from .relay import Decision, decision_input, same_decision_input, stamp_decision
    from copy import deepcopy
    generic = decision_input(decision)
    requested_decision = deepcopy(decision)
    capability = None
    paths = {row["objective"]: _path(project, row["objective"]) for row in requested}
    results = []
    with ExitStack() as stack:
        # Nothing is created for a name that has no record: a lock would make its directory.
        for path in sorted((path for path in paths.values() if path.exists()), key=str):
            stack.enter_context(_lock(path))
        if decision is not None and not generic and any(_older(path) for path in paths.values() if path.exists()):
            # An older record's entry certifies its retirement only with the decision's stored evidence and the
            # use receipt its publication writes, whoever decided (R205).
            if decision.get("channel", "session") != "session":
                raise PodError("retirement_decision_unverifiable",
                               "A superseded record's retirement needs a decision Pod stores with its evidence: the "
                               "person in session, a verified decision request, or the decision policy's agent "
                               "decider. Nothing was written.",
                               {"next_action": "decide in session, or relay the owner's answer through the decision "
                                               "verification action"})
            generic = True
        if generic:
            states, docs, planned = {}, {}, []
            for index, item in enumerate(requested):
                name, candidate_path = item["objective"], paths[item["objective"]]
                if not candidate_path.exists():
                    continue
                try:
                    saved, docs[name], _ = _load_record(candidate_path, project)
                except PodError:
                    continue  # The ordinary per-objective refusal below remains authoritative.
                states[name] = saved
                checked = _detect(project, name, item["release"], saved, port=port, repository=repository,
                                  host=_Host(project), doc=docs[name])
                block = decision["objectives"][index]
                if (checked["status"] == "eligible" and _block_valid(block, docs[name] is not None)
                        and {key: block.get(key) for key in FACTS} == checked["facts"] and not _moved(checked["locations"])):
                    planned.append((index, checked))
            recorded = next((saved[FIELD] for saved in states.values()
                             if isinstance(saved.get(FIELD), dict) and "decision_binding" in saved[FIELD]
                             and same_decision_input(decision, saved[FIELD])), None)
            if planned or recorded is not None:
                primary = recorded["decision_binding"]["resources"]["primary"] if recorded else requested[planned[0][0]]["objective"]
                resources = {"repository": repository, "objectives": digest([row["objective"] for row in requested]),
                             "records": digest(sorted(states)), "primary": primary}
                effect = {"decision": requested_decision}
                if recorded is None:
                    capability = Decision(project, primary, kind="retirement", effect=effect, state=states,
                                          decision=requested_decision, resources=resources)
                else:
                    if recorded["decision_binding"]["resources"] != resources:
                        raise PodError("decision_stale", "Retirement record scope changed; obtain a fresh scoped decision")
                    capability = (Decision.resume if planned else Decision.complete)(project, primary, recorded, effect=effect)
                capability.keys = [prefix + digest(requested[index]["objective"]) for index, _ in planned
                                   for prefix in ("ownership:", "retirement:")]
                capability.phases({name: paths[name] for name in states}, states)
                stack.enter_context(capability.scope())
                decision = owner_decision(decision, kind="retirement")
                capability.finalize(decision)
                # Re-observe every selected location/native scope after host lookup,
                # then preflight complete authority-bearing rows before the first receipt.
                from .history import prepare_context
                from .ledger import _validate_context
                from .util import protect_decision_record
                for index, checked in planned:
                    item = requested[index]
                    name = item["objective"]
                    fresh = _detect(project, name, item["release"], states[name], port=port,
                                    repository=repository, host=_Host(project), doc=docs[name])
                    if fresh["status"] != "eligible" or fresh["facts"] != checked["facts"] or _moved(fresh["locations"]):
                        raise PodError("decision_stale", "Retirement eligibility changed during verification")
                    probe = deepcopy(states[name])
                    probe[FIELD] = _retirement_entry(decision, requested, repository, fresh["facts"], fresh,
                                                     decision["objectives"][index], capability.commit_clock())
                    stamp_decision(probe)
                    protect_decision_record(probe)
                    if docs[name] is not None:
                        # An older record's bytes stay as they are; only its entry beside it is checked (R205).
                        validate_entry(probe[FIELD], older=True)
                        _older_document(probe, b"", repository)
                        continue
                    probe["revision"] += 1
                    _validate_context(probe)
                    prepare_context(paths[name], probe)
        for index, row in enumerate(requested):
            name, path = row["objective"], paths[row["objective"]]
            doc = data = None
            try:
                state, doc, data = _load_record(path, project) if path.exists() else (None, None, None)
            except PodError as exc:
                verdict = {"objective": name, "status": "refused", "condition": "scope_unreadable",
                           "facts": {"objective": name, "owner": None, "runs": [], "release": row["release"],
                                     "locations": [], "retained": [], "unrecorded_locations": None,
                                     "records": []},
                           "message": f"The objective's record is unreadable or unsupported ({exc.code}).",
                           "next_action": ("Use internal storage inspect and migrate for this exact project and objective "
                                           "with its source revision and SHA256 before retirement; conversion preserves all logical facts." if exc.code == "record_too_large"
                                           else "Historical retirement needs a readable current record.")}
            else:
                # Each objective reads the host afresh: an earlier objective's inventory is never its proof.
                verdict = _detect(project, name, row["release"], state, port=port, repository=repository,
                                  host=_Host(project), doc=doc)
                if (decision is None and verdict["status"] in ("eligible", "refused")
                        and verdict["condition"] not in _RECORD_REFUSALS):
                    # A delivered objective closes by evidence instead, outside the owner question (R147). The
                    # record checks above still precede every native read.
                    verdict = _delivered(project, verdict, port=port)
            verdict["record"] = record_name(project, context, name)
            if decision is None:
                if verdict["status"] in ("eligible", "refused") and "completion" not in verdict:
                    from .evidence_closure import LEAD_ACTION
                    # Early record-only refusals keep their precedence and perform no new native read.
                    action = "The project lead resolves this condition: " + verdict["next_action"] + " " + LEAD_ACTION
                    verdict.update(completion={"status": "unverified", "conditions": [{
                        "code": verdict["condition"], "status": "unverified", "message": verdict["message"]}]},
                        party="lead", next_action=action,
                        abandonment="R112 remains an optional abandonment route, never delivered history.")
                results.append(verdict)
                continue
            block = decision["objectives"][index]
            facts = {key: block.get(key) for key in FACTS}
            if verdict["status"] == "retired":
                if (state[FIELD]["decision"] == digest(decision) or "channel" not in state[FIELD]
                        and legacy_digest == state[FIELD]["decision"]):
                    results.append({**verdict, "status": "replayed",
                                    "message": "This decision's historical retirement is already recorded; "
                                               "nothing was written."})
                else:
                    results.append({**verdict, "status": "refused"})
                continue
            if verdict["status"] != "eligible":
                results.append(verdict)
                continue
            if not _block_valid(block, doc is not None) or facts != verdict["facts"]:
                # A decision made before R113 names no dispositions or reservations, so it never binds them (R113).
                changed = sorted(key for key in FACTS if facts[key] != verdict["facts"][key]) \
                    if _block_valid(block, doc is not None) else ["malformed"]
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
            entry = _retirement_entry(decision, requested, repository, facts, verdict, block,
                                      capability.commit_clock() if capability else datetime.now(timezone.utc).isoformat())
            state[FIELD] = entry
            if doc is not None:
                _write_older(path, state, data, repository)
            else:
                _write_retirement(path, state)
            results.append({**verdict, "status": "retired", "retirement": status_block(state),
                            "message": "Historical retirement recorded: the record is kept, not completed, and "
                                       "accepts no writes.",
                            "next_action": ACTION.format(name)})
    done = [row for row in results if row["status"] in ("retired", "replayed")]
    if decision is None:
        eligible = [row for row in results if row["status"] == "eligible"]
        delivered = [row["objective"] for row in results if row["status"] == "delivered"]
        tally = {"pending": 0, "unknown": 0, "failed": 0, "unreported": 0}
        for row in eligible:
            for key, value in counts(row["facts"]["records"]).items():
                tally[key] += value
        out = {"status": "retirement_available" if eligible else "retirement_unavailable",
               "repository": repository, "objectives": results}
        if delivered:
            out["delivered"] = delivered
        if eligible:
            out["question"] = _question(eligible, [row for row in results
                                                   if row["status"] not in ("eligible", "delivered")], tally)
            out["next_action"] = ("The project lead resolves every objective's completion.conditions, then runs pod sweep. "
                                  "Only for optional abandonment, name only the objectives the owner's answer names "
                                  "in an explicit user_direct decision of exactly {provenance, "
                                  "instruction, repository, reason, objectives}, each of those objectives in order as "
                                  "{objective, owner, runs, release, locations, retained, unrecorded_locations, "
                                  "records, delivery} with these facts and its delivery references; a refused "
                                  "objective stays refused alone; this route never confirms completed work.")
        else:
            out["next_action"] = "The project lead resolves each named completion condition, then runs pod sweep; historical retirement is optional abandonment."
        if delivered:
            out["next_action"] += (" Delivered " + ", ".join(delivered) + ": " + DELIVERED_ACTION
                                   + " (or rerun this request with proof: delivery).")
        return out
    return {"status": "retired" if len(done) == len(results) else "partially_retired" if done else "refused",
            "repository": repository, "objectives": results}

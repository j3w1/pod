"""Read-only loss detection and the sole explicit coordinator-change transition."""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import ExitStack
from copy import deepcopy
import os
import re
import stat
from pathlib import Path

from .errors import PodError
from .util import bounded_text, exact, owner_decision, CHANNEL_FIELDS

FACTS = {"objective", "from_owner", "to_owner", "run", "recorded_generation",
         "recorded_runtime", "current_runtime", "ambiguity"}
DECISION = FACTS | {"provenance", "instruction"}
ENTRY = {"provenance", "state", "from_runtime", "to_runtime", "at", "verified",
         "decision", "abort_reason", "completion"}


def validate_entry(row: dict) -> None:
    decision = row.get("decision")
    if (set(row) - {"resolution"} != ENTRY or row.get("state") not in ("pending", "done", "aborted")
            or not isinstance(row.get("verified"), dict)
            or not isinstance(decision, dict) or set(decision) - CHANNEL_FIELDS != DECISION
            or decision.get("provenance") not in ("user_direct", "agent_decision")
            or any(not isinstance(decision.get(key), str) or not decision[key]
                   for key in DECISION - {"ambiguity", "recorded_generation"})
            or (decision["recorded_generation"] is not None and
                (type(decision["recorded_generation"]) is not int or decision["recorded_generation"] < 0))
            or not isinstance(decision.get("ambiguity"), list)
            or any(not isinstance(item, str) for item in decision["ambiguity"])
            or row.get("abort_reason") is not None and not isinstance(row["abort_reason"], str)):
        raise PodError("state_unsupported", "Owner handoff history is malformed")
    owner_decision(decision, kind="handoff", stored=True)
    if "resolution" in row:
        from .relay import validate_decision_record
        validate_decision_record(row["resolution"], kind="handoff")
    # Presentation and pane identities are never accepted as authority or stored evidence.
    allowed = {"run", "coordinator", "generation", "admissions", "absent", "owner_code", "caller_runtime", "unlinked"}
    completion = row["completion"]
    if (set(row["verified"]) - allowed or
            any(part.get("unlinked", True) is not True for part in (row["verified"], completion) if isinstance(part, dict)) or
            (row["state"] == "done") != isinstance(completion, dict) or
            completion is not None and (not isinstance(completion, dict) or set(completion) - allowed)):
        raise PodError("state_unsupported", "Owner handoff evidence is unsupported")


def unfinished(state: dict) -> dict | None:
    history = ((state.get("checkpoint") or {}).get("continuity") or {}).get("history", [])
    rows = [row for row in history if row.get("provenance") == "owner_handoff" and row["state"] != "done"]
    if len(rows) > 1:
        raise PodError("state_unsupported", "More than one unfinished owner handoff")
    return rows[0] if rows else None


_SCHEMA_VERSION = re.compile(r"(pod-(?:context|checkpoint|admission))/v([0-9]{1,4})")


def _older_schema(raw: dict) -> bool:
    """Every non-current schema the record carries is an earlier version of a current family."""
    from .installer import STATE_SCHEMAS, superseded_schemas
    current = {}
    for name in STATE_SCHEMAS.values():
        match = _SCHEMA_VERSION.fullmatch(name)
        current[match.group(1)] = int(match.group(2))
    found = superseded_schemas(raw)
    for name in found:
        match = _SCHEMA_VERSION.fullmatch(name)
        if match is None or int(match.group(2)) >= current[match.group(1)]:
            return False
    return bool(found)


def _names(value: object, text: str, exact: bool, reader, depth: int = 0) -> bool:
    """Whether a decoded value names `text`: a key equal to it, or a string equal to it (`exact`) or containing it.
    A history reference is expanded through the record's own store; anything deeper than the parse bound names it."""
    from .history import _expand, is_reference
    from .history_tree import PARSE_DEPTH
    if depth >= PARSE_DEPTH:
        return True
    if is_reference(value):
        return _names(_expand(value, reader.read_bytes, {}), text, exact, reader, depth + 1)
    if isinstance(value, str):
        return value == text if exact else text in value
    if isinstance(value, dict):
        return any(key == text or _names(item, text, exact, reader, depth + 1) for key, item in value.items())
    if isinstance(value, list):
        return any(_names(item, text, exact, reader, depth + 1) for item in value)
    return False


def record_mentions(directory: Path, text: str, check=None, *, exact: bool = False) -> bool:
    """Whether a record could name `text`, such as an objective name (`exact`) or a Run id (R206).

    Membership is decided over the decoded logical record only: Pod's own bounded storage readers reconstruct it
    (descendant pages, chunk sequences and the preceding base64 chunk format), and every decoded key and string is
    compared. When the record cannot be reconstructed and parsed within those readers' byte, memory and time
    bounds, membership is unknown and the record counts as naming it, so it holds. Outer deadline exhaustion
    propagates as its own refusal."""
    from .history import ChunkStore, load_context, scan_read
    from .history_tree import Reader, plain
    from .orca import DEADLINE_SPENT
    if check is not None:
        check()

    def read():
        raw = load_context(Path(directory) / "context.json", validate=False)
        reader = Reader(ChunkStore(Path(directory)))
        try:
            return _names(plain(raw), text, exact, reader)
        finally:
            reader.close()
    try:
        # A record still changing after the bounded rereads has unknown membership, so it holds (R206).
        return scan_read(Path(directory) / "context.json", read,
                         faults=(PodError, OSError, RecursionError, ValueError, TypeError, KeyError, MemoryError))
    except PodError as exc:
        if exc.code == DEADLINE_SPENT:
            raise
        return True
    except (OSError, RecursionError, ValueError, TypeError, KeyError, MemoryError):
        return True


_PROOF_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")
# Fixed Pod-authored reasons a closed peer's closure stays unproven; never record contents or exception text (pod#250).
BINDING_UNAVAILABLE = "recorded repository binding unavailable"
WORKTREE_MISSING = "recorded worktree missing"
WORKTREE_UNREADABLE = "recorded worktree unreadable"
CLOSED_MALFORMED = "closed record malformed"
FORMAT_UNSUPPORTED = "record format unsupported"
# With the recorded worktree gone, the unchanged closure proof runs in another checkout of the peer's recorded repository,
# found in Pod's own records and confirmed by its repository key (pod#251).
MAX_CHECKOUTS = 8
MISSING = object()  # No checkout of the peer's repository was found.
NO_CHECKOUT = WORKTREE_MISSING + "; no other checkout of its recorded repository found"
RECORD_UNBOUND = WORKTREE_MISSING + "; record not where its recorded repository and objective place it"
RECORD_UNBOUND_BINDING = "record not where its recorded repository and objective place it"


def _unproven_reason(exc: BaseException) -> str:
    """The fixed reason one failed closure proof names; a PodError adds only its bounded code."""
    if isinstance(exc, PodError):
        code = exc.code if isinstance(exc.code, str) and _PROOF_CODE.fullmatch(exc.code) else "unknown"
        return f"closure proof refused ({code})"
    if isinstance(exc, OSError):
        return WORKTREE_UNREADABLE
    return CLOSED_MALFORMED


class _BindingChanged(Exception):
    """A removed-worktree peer whose recorded repository key changed after its checkout was chosen (pod#251)."""


class _Unproven(Exception):
    """A closure proof that cannot run, carrying its fixed reason."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def peer_label(row) -> str:
    """One peer named with its repository, so a same-named objective elsewhere never reads as this one (pod#251)."""
    if isinstance(row, str):  # A scope from an earlier shape names the objective alone.
        return row
    repository = row.get("repository")
    return f"{row['objective']} in " + (repository if repository else "an unrecorded repository")


def shared_detail(scope: dict, *, limit: int | None = None) -> str:
    """The run_shared detail both continuation paths word alike: open peers, then closed peers with an unproven
    closure and each fixed reason; an empty group is left out (pod#250). Each peer carries its repository (pod#251)."""
    opened = scope.get("open") or []
    unproven = scope.get("closure_unproven") or []
    groups = []
    if opened:
        groups.append("open: " + ", ".join(peer_label(row) for row in opened[:limit]))
    if unproven:
        groups.append("closed with unproven closure: "
                      + ", ".join(f"{peer_label(row)} ({row['reason']})" for row in unproven[:limit]))
    if not groups:  # A scope from an earlier shape: the union alone.
        groups.append(", ".join(scope["objectives"][:limit]))
    return "; ".join(groups)


def peer_list(rows: list, *, limit: int | None = None) -> str:
    """Peers named with their repositories, as the closure-unchecked and peer-history refusals list them."""
    return ", ".join(peer_label(row) for row in rows[:limit])


def shared_fields(scope: dict, *, limit: int | None = None) -> dict:
    """The run_shared result fields beside other_objectives: the same two groups, each capped alike."""
    return {"open_objectives": [dict(row) if isinstance(row, dict) else row
                                for row in (scope.get("open") or [])[:limit]],
            "unproven_closures": [dict(row) for row in (scope.get("closure_unproven") or [])[:limit]]}


def run_scope(project: Path, objective: str, run_id: str, *, budget=None, prove_closure: bool = True) -> dict:
    """Prove scope without the diagnostic reader's unsupported-record skip.

    A positive raw reference blocks unless a supported record proves closure.
    Unknown membership holds only for a record that could name this Run: one whose stored bytes mention it or cannot
    be read (R206). No record is converted, locked or rewritten.
    Discovery may supply a budget; exhaustion raises rather than returning a partial
    scope. Without it, continuation and owner handoff still require the complete proof.
    Discovery also passes `prove_closure=False`: a closed peer that records the Run is then
    reported in `closure_unchecked` instead of proving its closure, which only execution does.
    `objectives` is every blocking peer; `open` and `closure_unproven` split it into peers with no closure and closed
    peers whose closure proof failed or cannot run, each with a fixed reason (pod#250). Their rows, and
    `unverifiable_rows` and `closure_unchecked_rows` beside the name lists, carry each peer's recorded repository; a
    closed peer whose recorded worktree is gone runs the same closure proof in another checkout of its recorded
    repository: this project when it is one, else the first existing checkout another record binds (at most
    MAX_CHECKOUTS tried), each confirmed by its repository key, so the record's own Governor journal and Git history
    are read; with none the peer stays unproven (pod#251).
    """
    from .ledger import (CONTEXT_LIMIT, _validate_context, validate_checkpoint_snapshot,
                         _run_references, objective_root, record_name, state_root, closed_snapshot_context)
    from .history import RECORD_CHANGED, load_context, scan_read
    from .orca import DEADLINE_SPENT

    def check():
        if budget is not None:
            budget.check()

    def directory_entries(path):
        check()
        with os.scandir(path) as entries:
            found = []
            while True:
                check()
                entry = next(entries, None)
                if entry is None:
                    return found
                found.append(entry)

    check()
    root = state_root(project)
    check()
    if root.is_symlink():
        return {"objectives": [], "open": [], "closure_unproven": [], "unreadable": 1}
    own = objective_root(project, objective) / "context.json"
    own_repository = []

    def repository_of(checkpoint, path):
        """The peer's recorded repository: its worktree binding's, else this project's for a record in this
        project's own directory, else None."""
        from .records import worktree_binding
        try:
            if "worktree" in checkpoint:
                return worktree_binding(checkpoint["worktree"]).get("repository")
            name = checkpoint["objective"]
            if objective_root(project, name) != path.parent:
                return None
            if not own_repository:
                from .github import repository_context
                own_repository.append(repository_context(project).get("repository"))
            return own_repository[0]
        except (PodError, KeyError, TypeError, ValueError, AttributeError, OSError) as exc:
            if isinstance(exc, PodError) and exc.code == DEADLINE_SPENT:
                raise
            return None
    others, unknown = [], 0
    opened, closed = [], []
    unverifiable, unchecked = [], []
    # A complete frontier is part of the proof. scandir/stat propagate errors that
    # Path.glob suppresses; an unavailable directory is never apparent absence.
    paths = []
    try:
        directories = directory_entries(root)
    except FileNotFoundError:
        return {"objectives": [], "open": [], "closure_unproven": [], "unreadable": 1}
    except OSError:
        return {"objectives": [], "open": [], "closure_unproven": [], "unreadable": 1}
    for directory in directories:
        check()
        try:
            if directory.is_symlink():
                unknown += 1  # A redirected record directory cannot be read in place.
                continue
            check()
            if not directory.is_dir(follow_symlinks=False):
                continue
            entries = directory_entries(directory.path)
            paths.extend(Path(entry.path) for entry in entries if entry.name == "context.json")
        except OSError:
            unknown += 1
    checkouts = {}  # repo_key -> the worktree paths readable records bind (pod#251)

    def classify(path, substitute=None):
        """One record's whole read: what it says about this Run, or None when it holds nothing. A failure to load the
        record is marked, so its hold is narrowed by membership as before; every other record-local fault holds.
        `substitute` is (checkout, repository key it was chosen for): the checkout of the peer's repository its closure
        proof runs in when its worktree is gone."""
        try:
            raw = load_context(path, validate=False)
        except PodError as exc:
            exc.scan_unloaded = True
            raise
        if not isinstance(raw, dict):
            return "mentions", None
        # Every lazy read of this record stays inside its own boundary: a missing page makes its membership
        # unknown, which holds this Run, and never aborts the scan of other records (R206).
        checkpoint = raw.get("checkpoint")
        bound = checkpoint.get("worktree") if isinstance(checkpoint, dict) else None
        if isinstance(bound, dict) and isinstance(bound.get("repo_key"), str) and isinstance(bound.get("path"), str):
            checkouts.setdefault(bound["repo_key"], set()).add(bound["path"])
        admissions = raw.get("admissions")
        refs = checkpoint.get("native_refs") if isinstance(checkpoint, dict) else [] if checkpoint is None else None
        readable = (isinstance(refs, list) and isinstance(admissions, dict)
                    and all(isinstance(row, dict) and isinstance(row.get("runId"), str) and row["runId"] for row in refs)
                    and all(isinstance(row, dict) and isinstance(row.get("run_id"), str) and row["run_id"] for row in admissions.values()))
        referenced = (isinstance(refs, list) and any(isinstance(row, dict) and row.get("runId") == run_id for row in refs)
                      or isinstance(admissions, dict) and any(isinstance(row, dict) and row.get("run_id") == run_id for row in admissions.values()))
        supported = False
        check()
        try:
            _run_references(_validate_context(raw))
            supported = True
        except PodError:
            pass
        if referenced:
            closure = checkpoint.get("closure") if isinstance(checkpoint, dict) else None
            repository = repository_of(checkpoint, path) if isinstance(checkpoint, dict) else None
            if not (isinstance(repository, str) and 0 < len(repository) <= 256):
                repository = None
            if supported and closure is not None and not prove_closure:
                name = checkpoint["objective"]
                return ("unchecked", name if isinstance(name, str) and 0 < len(name) <= 256 else "unnamed objective",
                        repository)
            reason = None  # None: the peer records no closure, so it is open.
            if supported and closure is not None:
                from .obligations import validate_closed_snapshot
                try:
                    # Only a complete immutable consumed observation proves past
                    # settlement for closure; unreported work stays outstanding.
                    from .records import worktree_binding
                    workspace = worktree_binding(checkpoint["worktree"]) if "worktree" in checkpoint else None
                    peer_project = Path(workspace["path"]) if workspace is not None else (
                        project if objective_root(project, checkpoint["objective"]) == path.parent else None)
                    if peer_project is None:
                        raise _Unproven(BINDING_UNAVAILABLE)
                    check()
                    gone = False
                    if substitute is not None:
                        # A deferred peer's reread joins the key its checkout was chosen for before anything else,
                        # whether its path is still missing or now exists: a changed binding is being written, so it
                        # holds and is retried, never proved (R206).
                        place, chosen_for = substitute
                        if workspace is None or workspace["repo_key"] != chosen_for:
                            raise _BindingChanged()
                    # The binding must place this record where it lies, so the proof reads the record's own journal
                    # (pod#251).
                    if workspace is not None and record_name(
                            Path(workspace["path"]), {"repo_key": workspace["repo_key"]},
                            checkpoint["objective"]) != path.parent.name:
                        raise _Unproven(RECORD_UNBOUND_BINDING)
                    try:
                        found = peer_project.stat()
                    except FileNotFoundError:
                        if workspace is None:
                            raise _Unproven(WORKTREE_MISSING) from None
                        if substitute is None:
                            # Normal completion cleanup removed the worktree: the proof runs after the scan, in another
                            # checkout of the same repository (pod#251).
                            name = checkpoint["objective"]
                            return ("gone", name if isinstance(name, str) and 0 < len(name) <= 256
                                    else "unnamed objective", repository, workspace["repo_key"])
                        if place is MISSING:
                            raise _Unproven(NO_CHECKOUT) from None
                        gone, peer_project = True, place
                    except OSError:
                        raise _Unproven(WORKTREE_UNREADABLE) from None
                    else:
                        if not stat.S_ISDIR(found.st_mode):
                            raise _Unproven(WORKTREE_UNREADABLE)
                    try:
                        # The checkout must place this record where it lies too.
                        if gone and objective_root(peer_project, checkpoint["objective"]) != path.parent:
                            raise _Unproven(RECORD_UNBOUND)
                        check()
                        validate_checkpoint_snapshot(peer_project, raw)
                        check()
                        ctx = closed_snapshot_context(peer_project, checkpoint["objective"], raw)
                        check()
                        unproven = validate_closed_snapshot(checkpoint, ctx)
                    except (PodError, KeyError, TypeError, ValueError, OSError) as exc:
                        if isinstance(exc, PodError) and exc.code == DEADLINE_SPENT or not gone:
                            raise
                        raise _Unproven(WORKTREE_MISSING + "; " + _unproven_reason(exc)) from None
                except _Unproven as exc:
                    reason = exc.reason
                except (PodError, KeyError, TypeError, ValueError, OSError) as exc:
                    if isinstance(exc, PodError) and exc.code == DEADLINE_SPENT:
                        raise
                    reason = _unproven_reason(exc)
                else:
                    if unproven:
                        name = checkpoint["objective"]
                        return ("unverifiable",
                                name if isinstance(name, str) and 0 < len(name) <= 256 else "unnamed objective",
                                repository)
                    return None
            elif closure is not None:
                reason = FORMAT_UNSUPPORTED  # A closure Pod cannot read in a format it does not support.
            name = checkpoint.get("objective") if isinstance(checkpoint, dict) else None
            return ("other", (name if isinstance(name, str) and name and len(name) <= 256 else "unnamed objective"),
                    reason, repository)
        if (not readable or not supported) and record_mentions(path.parent, run_id, check):
            # A record outside the documented reference layouts holds this Run only when its decoded record names
            # it, or cannot be decoded within bounds (R206).
            return "unknown", None
        return None

    changing = 0
    gone = []

    def take(path, substitute=None):
        nonlocal changing, unknown
        try:
            # The record's read and its classification, classified by revision (R206).
            outcome = scan_read(path, lambda: classify(path, substitute))
        except _BindingChanged:
            # Being written, not damaged: it holds like a record that changed while it was read, with a retry step.
            changing += record_mentions(path.parent, run_id, check) or 1
            return
        except PodError as exc:
            if exc.code == DEADLINE_SPENT:
                raise
            if exc.code == RECORD_CHANGED:
                # Live, not damaged: it holds this Run on the membership rule of a record that cannot be read, with
                # its own transient cause and a retry step.
                changing += record_mentions(path.parent, run_id, check)
            elif getattr(exc, "scan_unloaded", False):
                unknown += record_mentions(path.parent, run_id, check)
            else:
                unknown += 1
            return
        except (KeyError, TypeError, ValueError, AttributeError, RecursionError):
            unknown += 1
            return
        if outcome is None:
            return
        kind, name = outcome[:2]
        if kind == "gone":
            gone.append((path, outcome[3]))
        elif kind == "mentions":
            unknown += record_mentions(path.parent, run_id, check)
        elif kind == "unknown":
            unknown += 1
        elif kind == "other":
            others.append(name)
            if outcome[2] is None:
                opened.append((name, outcome[3] or ""))
            else:
                closed.append((name, outcome[3] or "", outcome[2]))
        else:
            {"unchecked": unchecked, "unverifiable": unverifiable}[kind].append((name, outcome[2] or ""))

    for path in paths:
        check()
        if path != own:
            take(path)
    found_keys = {}

    def same_repository(place, key):
        """Whether an existing directory is a checkout of the repository with this key, read from Git."""
        if place not in found_keys:
            check()
            try:
                from .github import repository_context
                found_keys[place] = repository_context(place)["repo_key"] if place.is_dir() else None
            except (PodError, OSError, KeyError, TypeError, ValueError) as exc:
                if isinstance(exc, PodError) and exc.code == DEADLINE_SPENT:
                    raise
                found_keys[place] = None
        return found_keys[place] == key

    def existing(place):
        check()
        try:
            return place.is_dir()
        except OSError:
            return False
    for path, key in gone:
        # This project first, then the other checkouts records bind, in a fixed order; the first that is a checkout
        # of the same repository is the one the proof runs in, never a search for one where it passes.
        # Removed worktrees are dropped by a plain stat before the cap, so the cap counts only existing checkouts
        # that Git is asked about (pod#251: most recorded paths of a busy repository are cleaned up).
        places = [Path(project)] + [Path(place) for place in sorted(checkouts.get(key, ()))
                                    if Path(place) != Path(project) and existing(Path(place))]
        # A binding without a repository key (a plain directory) has no other checkout.
        chosen = next((place for place in places[:MAX_CHECKOUTS + 1] if same_repository(place, key)),
                      MISSING) if isinstance(key, str) else MISSING
        take(path, (chosen, key))
    check()

    def rows(found):
        """One row per peer and repository, so same-named objectives of two repositories stay apart (pod#251)."""
        return [{"objective": name, "repository": repository or None} for name, repository in sorted(set(found))]
    reasons = {}
    for name, repository, reason in sorted(closed):
        reasons.setdefault((name, repository), reason)  # One row per peer.
    result = {"objectives": sorted(set(others)), "open": rows(opened),
              "closure_unproven": [{"objective": name, "repository": repository or None, "reason": reason}
                                   for (name, repository), reason in sorted(reasons.items())],
              "unreadable": unknown, "unverifiable": sorted({name for name, _ in unverifiable}),
              "unverifiable_rows": rows(unverifiable)}
    if changing:
        result["changing"] = changing
    if unchecked:
        result["closure_unchecked"] = sorted({name for name, _ in unchecked})
        result["closure_unchecked_rows"] = rows(unchecked)
    return result


SUCCESSOR = "Start a successor objective that claims this one; do not run orca orchestration run-use by hand."


def recorded_place(project: Path | None, checkpoint: dict, *, caller: bool = True) -> dict | None:
    """Where a new terminal takes the handoff (pod#292): None when the recorded worktree still holds its recorded
    repository, key, path and branch and, with `caller`, this terminal runs there; else the failing condition.

    Status asks without a caller, so it offers the route only where a terminal could take it."""
    from .operations import _check_worktree_binding
    from .window import _cwd_toplevel, _worktree_directory
    workspace = checkpoint.get("worktree")
    if not isinstance(workspace, dict) or not isinstance(workspace.get("path"), str) or not workspace["path"]:
        return {"condition": "caller_not_linked", "action": SUCCESSOR,
                "message": "The caller has no stable current Run binding, and the objective records no worktree "
                           "in which a new terminal could take the handoff."}
    recorded = Path(workspace["path"])
    here = ("Run the handoff from a live terminal in the recorded worktree " + workspace["path"]
            + ", or start a successor objective; do not run orca orchestration run-use by hand.")
    try:
        if not _worktree_directory(recorded):
            return {"condition": "worktree_changed", "action": SUCCESSOR,
                    "message": "The recorded worktree is missing, so no new terminal can take the handoff."}
        _check_worktree_binding(recorded, workspace)
    except PodError as exc:
        if exc.code != "worktree_binding_changed":
            return {"condition": "worktree_unreadable", "unverified": True,
                    "message": "The recorded worktree could not be read (" + exc.code + ")."}
        return {"condition": "worktree_changed", "action": SUCCESSOR,
                "message": "The recorded worktree's repository, key or branch differs from the record, "
                           "so no new terminal can take the handoff there."}
    if not caller:
        return None
    try:
        _check_worktree_binding(project, workspace)
    except PodError:
        return {"condition": "worktree_changed", "action": here,
                "message": "This terminal's repository, branch or worktree differs from the record."}
    top = _cwd_toplevel()
    if top is None or Path(top).resolve() != recorded.resolve():
        return {"condition": "worktree_changed", "action": here,
                "message": "This terminal does not run in the recorded worktree, where a handoff without a "
                           "current Run must run."}
    return None


def _unlinked_run(project: Path, checkpoint: dict, run_id: str | None, runtime: str) -> dict:
    """For a caller with no current Run: the recorded worktree, then the Run's coordinator by run show (pod#292)."""
    from .orca import run_show
    place = recorded_place(project, checkpoint)
    if place is not None:
        return place
    if run_id is None:
        return {"condition": "caller_not_linked", "action": SUCCESSOR,
                "message": "The caller has no stable current Run binding, and the objective records no single Run to bind."}
    try:
        shown = run_show(run_id)
    except PodError as exc:
        return {"condition": "run_unreadable", "message": "The recorded Run could not be read (" + exc.code + ")."}
    if shown["runtime"] != runtime or not shown["coordinator_known"]:
        return {"condition": "run_unreadable", "message": "The Run read does not prove its coordinator at the caller's runtime."}
    return {"shown": shown}


def _shown_again(run_id: str) -> dict | None:
    from .orca import run_show
    try:
        return run_show(run_id)
    except PodError:
        return None


def detect(project: Path, objective: str, state: dict, *, port, caller: str | None = None) -> dict:
    """Fresh native facts only. No writes, native mutations or presentation data."""
    from .ledger import _run_references, continuity_work
    from .orca import current_run, terminal_identity, worker_rows
    caller = os.environ.get("ORCA_TERMINAL_HANDLE") if caller is None else caller
    owner = state.get("owner")
    checkpoint = state.get("checkpoint") or {}
    binding = (checkpoint.get("continuity") or {}).get("binding")
    refs = _run_references(state)
    run_id = binding["run"] if binding else next(iter(refs)) if len(refs) == 1 else None
    generation = binding["generation"] if binding else None
    facts = {"objective": objective, "from_owner": owner, "to_owner": caller, "run": run_id,
             "recorded_generation": generation,
             "recorded_runtime": next(iter(set(refs.values()))) if len(set(refs.values())) == 1 else None,
             "current_runtime": None, "ambiguity": []}
    result = {"status": "handoff_unavailable", "condition": "caller_not_linked", "facts": facts,
              "definitive": False, "verified": {}}

    def fail(condition: str, message: str, *, definitive=False, action=None) -> dict:
        result.update(condition=condition, message=message, definitive=definitive,
                      next_action=action or "Continue from the original pane, or start a successor objective. Do not run orca orchestration run-use by hand.")
        return result

    link = state.get("continued_by")
    if isinstance(link, dict):
        from .succession import ACTION
        return fail("objective_continued", f"The objective was continued by successor objective {link['objective']}.",
                    definitive=True, action=ACTION.format(link["objective"]))
    if isinstance(state.get("historical_retirement"), dict):
        from .retirement import named, retired_action
        entry = state["historical_retirement"]
        return fail("objective_retired", f"{named(entry, capital=True)} ended this objective's coordination; "
                    "it accepts no coordinator.", definitive=True, action=retired_action(objective, entry))
    from .evidence_closure import present
    from .ledger import objective_root
    if present(objective_root(project, objective)):
        return fail("objective_closed", "The objective was closed by evidence, coordinator gone.", definitive=True,
                    action="Read it with pod status --objective " + objective + "; it accepts no coordinator.")
    if caller == owner:
        return fail("recorded_owner", "This caller is the recorded owner; use the ordinary authority and runtime-continuity checks.")
    if facts["recorded_runtime"] is None:
        return fail("recorded_runtime_unreadable", "The objective has no single readable recorded runtime.")
    if checkpoint.get("closure"):
        return fail("objective_closed", "The objective is closed.", definitive=True,
                    action="Start a successor objective; closure does not authorize a handoff.")
    if not caller:
        return fail("caller_not_live", "This process is not in a live Orca terminal; observe with pod status --objective ID.")
    own = terminal_identity(caller)
    if own["state"] != "live":
        return fail("caller_not_live", "Orca cannot prove this caller's handle live (" + (own["code"] or "successful terminal show did not prove liveness") + ").",
                    definitive=own["state"] == "lost")
    pending = unfinished(state)
    from .window import unfinished as window_unfinished
    window = window_unfinished(state)
    if window is not None and window["state"] == "pending":
        # Neither coordinator-change path overwrites the other's pending authority (pod#110).
        return fail("window_continuation_pending", "A window continuation is pending; it finishes through pod continue.",
                    action="Finish the pending window continuation with pod continue --objective " + objective
                           + " --json; do not run orca orchestration run-use by hand.")
    if pending and pending["state"] == "pending" and pending["decision"]["to_owner"] != caller:
        holder = terminal_identity(pending["decision"]["to_owner"])
        if holder["state"] != "lost":
            return fail("pending_other_caller", "The pending handoff belongs to another terminal whose handle Orca has not reported stale or gone.",
                        action="Complete it in that terminal, or wait until Orca reports its handle stale or gone; do not run orca orchestration run-use by hand.")
        # Detection cannot abort; the operation alone records and replaces this attempt.
        result["replace_pending"] = "pending caller handle stale or gone"
    try:
        first = current_run()
    except PodError as exc:
        return fail("binding_unreadable", "The caller's current Run binding could not be read (" + exc.code + ").")
    facts["current_runtime"] = first["runtime"]
    run = first["run"]
    if own["runtime"] != first["runtime"]:
        return fail("caller_not_linked", "The caller has no stable current Run binding at its live runtime.")
    if pending and pending["state"] == "pending" and pending["verified"].get("unlinked") is True:
        # A handoff begun without a current Run is finished, replayed or replaced only from its recorded worktree,
        # checked before any caller-relative refusal could read as a definite loss (pod#292).
        place = recorded_place(project, checkpoint)
        if place is not None:
            return fail(place["condition"], place["message"], action=place.get("action"))
    shown = None
    if run is None:
        # A new terminal has no current Run (pod#292). It may bind the recorded Run inside the handoff itself, as the
        # window continuation does, only from the recorded worktree and only while run show names the recorded owner.
        unlinked = _unlinked_run(project, checkpoint, run_id, first["runtime"])
        if "condition" in unlinked:
            return fail(unlinked["condition"], unlinked["message"], action=unlinked.get("action"))
        shown = unlinked["shown"]
        run = {"id": run_id, "coordinator_handle": shown["coordinator_handle"],
               "consumer_generation": shown["consumer_generation"]}
        result["unlinked"] = True
        if run["coordinator_handle"] == caller:
            # Orca names this terminal the Run's coordinator but not its current Run: never a definite loss.
            return fail("binding_unstable", "Orca shows this terminal coordinating the Run without a current Run binding; re-read status.")
        if run["coordinator_handle"] != owner:
            # Only the fresh branch can start unlinked; a resume always reads its own rebound current Run.
            return fail("lineage_unproven", "The Run was rebound outside a live matching pending handoff; lineage is unproven.", definitive=True,
                        action="Continue in a successor or recovery objective; do not run orca orchestration run-use by hand.")
    if run_id is None or run["id"] != run_id:
        return fail("different_run", "The caller resolves a different Run from the recorded objective.", definitive=True)
    if run["coordinator_handle"] == owner:
        branch = "fresh"
        if generation is not None and run["consumer_generation"] != generation:
            return fail("generation_changed", "The recorded owner's consumer generation differs.", definitive=True,
                        action="Continue in a successor or recovery objective; do not run orca orchestration run-use by hand.")
    elif (run["coordinator_handle"] == caller and pending and pending["state"] == "pending"
          and all(pending["decision"][key] == facts[key] for key in FACTS - {"ambiguity", "current_runtime"})):
        branch = "resume"
        if pending["verified"].get("unlinked") is True:
            result["unlinked"] = True  # Its recorded-worktree fence was checked above.
        if generation is not None and run["consumer_generation"] <= generation:
            return fail("generation_changed", "The rebound Run has no generation above the recorded one.", definitive=True,
                        action="Continue in a successor or recovery objective; do not run orca orchestration run-use by hand.")
    else:
        return fail("lineage_unproven", "The Run was rebound outside a live matching pending handoff; lineage is unproven.", definitive=True,
                    action="Continue in a successor or recovery objective; do not run orca orchestration run-use by hand.")
    previous = terminal_identity(owner)
    if previous["state"] != "lost":
        return fail("owner_not_lost", "Orca has not reported the recorded owner's handle stale or gone (" + (previous["code"] or previous["state"]) + ").",
                    action="Use the recorded coordinator, or resolve the unreadable handle state; do not run orca orchestration run-use by hand.")
    if any((row.get("native_binding") or {}).get("terminalHandle") == caller
           for row in state["admissions"].values()):
        return fail("caller_is_worker", "The caller is a worker terminal of this objective.", definitive=True)
    try:
        fleet = worker_rows(run_id)
    except PodError as exc:
        return fail("workers_unreadable", "The Run's worker list could not be read (" + exc.code + ").")
    if (fleet["runtime"] != first["runtime"] or fleet.get("complete") is not True
            or any(not isinstance(row, dict) for row in fleet["workers"])):
        return fail("workers_unreadable", "The worker list does not prove complete current-runtime scope.")
    if any(part.get(key) == caller for row in fleet["workers"]
           for part in (row, row.get("worker"), row.get("projection")) if isinstance(part, dict)
           for key in ("terminalHandle", "agentTerminalHandle")):
        return fail("caller_is_worker", "The caller has a worker-only binding on this Run.", definitive=True)
    scope = run_scope(project, objective, run_id)
    if scope["objectives"]:
        result["other_objectives"] = scope["objectives"]
        result.update(shared_fields(scope, limit=8))
        return fail("run_shared", "Another open or unverified-closed objective records this Run: " + shared_detail(scope) + ".", definitive=True,
                    action="Resolve the shared objective scope before handoff; do not run orca orchestration run-use by hand.")
    if scope["unreadable"]:
        result["unreadable_objectives"] = scope["unreadable"]
        return fail("scope_unreadable", "Run scope is unverified: " + str(scope["unreadable"]) + " objective record(s) could not prove membership or closure.",
                    action="Resolve the unreadable objective records before handoff; do not run orca orchestration run-use by hand.")
    if scope.get("changing"):
        from .history import RECORD_CHANGED
        result["changing_objectives"] = scope["changing"]
        return fail(RECORD_CHANGED, "Run scope is unverified: " + str(scope["changing"]) + " objective record(s) changed "
                    "while they were read; they are being written, not damaged.",
                    action="Retry the handoff detection; do not run orca orchestration run-use by hand.")
    if scope.get("unverifiable"):
        names = scope["unverifiable"]
        listed = scope.get("unverifiable_rows") or names  # Each peer with its repository (pod#251).
        result["unverifiable_peers"] = names[:8]
        result["remaining_peers"] = max(0, len(names) - 8)
        return fail("peer_history_unverifiable", "Handoff holds: the original eligibility scope of retained REUSE cannot be verified for peer "
                    + peer_list(listed, limit=8) + ("; and " + str(len(listed) - 8) + " more" if len(listed) > 8 else "") + ".",
                    action="Resolve the named peer history before handoff; ordinary writes remain available. No confirmation overrides this hold; do not run orca orchestration run-use by hand.")
    work = continuity_work(state, runtime=first["runtime"], port=port)
    if work["differs"]:
        result["differs"] = work["differs"]
        return fail("identity_differs", "A bound admission identity differs: " + ", ".join(work["differs"]) + ".", definitive=True,
                    action="Continue in a successor or recovery objective; differing identities cannot be overridden. Do not run orca orchestration run-use by hand.")
    try:
        after = current_run()
    except PodError:
        after = None
    if after != first or shown is not None and _shown_again(run_id) != shown:
        return fail("binding_unstable", "The caller's Run binding or runtime changed during detection.")
    facts["ambiguity"] = work["ambiguity"] + (["recorded consumer generation (none recorded)"] if binding is None else [])
    facts["ambiguity"] += [f"run {other}: not shown by run-current" for other in sorted(set(refs) - {run_id})]
    from .ledger import CONTINUITY_AMBIGUITY
    if len(facts["ambiguity"]) > CONTINUITY_AMBIGUITY:
        return fail("ambiguity_full", "Too many unreadable identities to bind an exact confirmation.")
    history = (checkpoint.get("continuity") or {}).get("history", [])
    question = (f"May Pod make this terminal the coordinator of objective {objective}? "
                "Orca reports the previous coordinator's handle stale or gone; the objective's owner will change and its work will be preserved.")
    result.update(status="handoff_available", condition=branch, question=question,
                  message="Caller is not the recorded owner; an Owner-authorized handoff is available."
                          + (" Ambiguity: " + "; ".join(facts["ambiguity"]) if facts["ambiguity"] else ""),
                  next_action="Ask the Owner: " + question + " Only their exact user_direct confirmation permits internal owner-handoff using these fresh facts; do not run orca orchestration run-use by hand.",
                  verified={**work["verified"], "run": run_id, "coordinator": run["coordinator_handle"],
                            "generation": run["consumer_generation"], "owner_code": previous["code"],
                            "caller_runtime": own["runtime"], **({"unlinked": True} if result.get("unlinked") else {})})
    return result


def nonowner_detection(project: Path, objective: str, state: dict | None, owner: str, *, port=None, current=None) -> dict | None:
    """Use the authority port's actual caller; supplied owner strings grant nothing."""
    from .ledger import _run_references
    from .operations import OrcaPort
    if not state or not state.get("owner"):
        return None
    # Recorded-owner calls keep precisely their existing reads and R98 behavior.
    if current is None and os.environ.get("ORCA_TERMINAL_HANDLE") == state["owner"]:
        return None
    port = port or OrcaPort(project)
    if current is None:
        try:
            current = port.read_native(owner, authority_runs=tuple(sorted(_run_references(state))))
        except PodError:
            current = {"caller": os.environ.get("ORCA_TERMINAL_HANDLE")}
    if "caller" not in current or current["caller"] == state["owner"]:
        return None
    return detect(project, objective, state, port=port, caller=current["caller"])


def refuse_nonowner(project: Path, objective: str, state: dict | None, owner: str, *, code="native_authority_unverified", port=None, current=None,
                    claim: str | None = None, reserved: bool = True) -> None:
    from .succession import refuse_continued, refuse_incomplete
    # Any caller, owner or not, is told the objective was continued (R110), or that an incomplete claim names it as
    # successor, whatever writer left that claim (R118).
    refuse_continued(state, claim=claim, reserved=reserved)
    from .evidence_closure import refuse_objective
    refuse_objective(project, objective)  # Closed by evidence: no coordinator, owner or not, writes it (R145).
    if reserved:
        refuse_incomplete(project, objective, state, claim=claim)
    verdict = nonowner_detection(project, objective, state, owner, port=port, current=current)
    if verdict is not None:
        detail = {"owner_handoff": verdict, "next_action": verdict["next_action"]}
        from .window import primary_action, status_block
        window = status_block(project, objective, state, port=port)
        if window is not None:
            # The window route, or its lead task, needs no owner decision and comes first (pod#110).
            detail["window_continuation"] = window
            detail["next_action"] = primary_action(window)
        raise PodError(code, "Caller is not the recorded owner. " + verdict["message"], detail)


def _preflight_decision(path, state, value, verified, pending, capability):
    """Both physical phases fit before run-use; the numeric allowance is never stored."""
    import sys
    from .history import prepare_context
    from .ledger import _validate_context
    from .relay import stamp_decision
    from .util import MAX_RECORD, protect_decision_record
    at = capability.commit_clock()
    staged = deepcopy(state)
    record = staged["checkpoint"].setdefault("continuity", {"binding": None, "history": []})
    row = {**deepcopy(pending or {}), "provenance": "owner_handoff", "state": "pending",
           "from_runtime": value["recorded_runtime"], "to_runtime": value["current_runtime"], "at": at,
           "verified": deepcopy(verified), "decision": dict(value), "abort_reason": (pending or {}).get("abort_reason"), "completion": None}
    if pending is not None and pending["decision"].get("decision_binding") != capability.binding:
        row["resolution"] = dict(capability.record)
    if pending is None:
        record["history"].append(row)
    else:
        index = state["checkpoint"]["continuity"]["history"].index(pending)
        record["history"][index] = row
    future = deepcopy(staged)
    digits = sys.get_int_max_str_digits() or MAX_RECORD
    generation = 10 ** (min(digits, MAX_RECORD) - 1)
    completed = future["checkpoint"]["continuity"]["history"][record["history"].index(row)]
    completed.update(state="done", completion={**deepcopy(verified), "coordinator": value["to_owner"], "generation": generation})
    future["owner"] = value["to_owner"]
    for admission in future["admissions"].values():
        admission.update(owner=value["to_owner"], runtime=value["current_runtime"])
    for ref in future["checkpoint"].get("native_refs", []):
        ref["runtime"] = value["current_runtime"]
    future["checkpoint"]["continuity"]["binding"] = {"run": value["run"], "coordinator": value["to_owner"], "generation": generation}
    for candidate in (staged, future):
        stamp_decision(candidate)
        protect_decision_record(candidate)
        candidate["revision"] += 2
        _validate_context(candidate)
        prepare_context(path, candidate)


def owner_handoff(project: Path, objective: str, *, owner: str, decision: object, port) -> dict:
    from .ledger import (_lock, _path, _read, _write, _require_open, CONTINUITY_HISTORY)
    from .orca import FULL_JSON, current_run, mutate_command, terminal_identity
    value = exact(decision, DECISION | CHANNEL_FIELDS, DECISION, name="handoff_decision")
    from .relay import Decision, decision_input, _raw_decision, same_decision_input
    generic = decision_input(value)
    requested_value = deepcopy(value)
    bounded_text(value["instruction"], name="instruction", limit=1024)
    if value["provenance"] != "user_direct" and not generic:
        raise PodError("handoff_decision_mismatch", "Only an exact direct Owner confirmation permits this handoff")
    if generic:
        _raw_decision(value)
    else:
        value = owner_decision(value, kind="handoff")
    path = _path(project, objective)
    capability = None

    def refusal(verdict, code="owner_handoff_refused"):
        return PodError(code, verdict["message"], {"owner_handoff": verdict, "next_action": verdict["next_action"]})

    def abort(entry, reason, state):
        if generic and capability is None:
            if "decision_binding" in entry["decision"] and same_decision_input(requested_value, entry["decision"]):
                authorize(entry["decision"], completion=True)
            else:
                authorize()
                entry["resolution"] = dict(capability.record)
        if capability is not None:
            capability.completion_only = True
        entry.update(state="aborted", abort_reason=reason)
        _write(path, state)

    # Keep the objective lock through the single native mutation and authoritative re-read.
    with _lock(path), ExitStack() as decision_stack:
        state = _read(path)
        def authorize(recorded=None, *, completion=False):
            nonlocal capability, value
            effect = {"decision": requested_value}
            states = {"objective": deepcopy(state)}
            if recorded is None:
                capability = Decision(project, objective, kind="handoff", effect=effect, state=states,
                                      decision=requested_value, resources={"objective": objective, "run": value["run"],
                                      "from_owner": value["from_owner"], "to_owner": value["to_owner"]})
            else:
                capability = (Decision.complete if completion else Decision.resume)(project, objective, recorded, effect=effect)
            from .util import digest
            capability.keys = ["ownership:" + digest(objective)]
            capability.phases({"objective": path}, states)
            decision_stack.enter_context(capability.scope())
            value = owner_decision(value, kind="handoff")
            capability.finalize(value)
            if not completion:
                refreshed = detect(project, objective, state, port=port)
                if refreshed["status"] != "handoff_available" or refreshed["facts"] != verdict["facts"]:
                    raise PodError("handoff_decision_mismatch", "Handoff facts changed during decision verification")
                _preflight_decision(path, state, value, refreshed["verified"], pending, capability)
        # A handoff moves coordination, not work: a successor an incomplete claim reserves may change coordinator
        # so that the new one finishes the claim (R118).
        _require_open(state, reserved=False)
        from .evidence_closure import refuse_objective
        refuse_objective(project, objective)
        if state.get("checkpoint") is None or owner != os.environ.get("ORCA_TERMINAL_HANDLE"):
            raise PodError("native_authority_unverified", "Handoff needs this live caller and a recorded objective")
        pending = unfinished(state)
        verdict = detect(project, objective, state, port=port)
        if any(value[key] != verdict["facts"][key] for key in FACTS):
            raise refusal({**verdict, "message": "The confirmation differs from fresh objective, owner, Run, generation, runtime or ambiguity facts. Nothing was written."}, "handoff_decision_mismatch")
        if pending and pending["state"] == "pending":
            holder = pending["decision"]["to_owner"]
            if holder != owner:
                if terminal_identity(holder)["state"] != "lost":
                    raise refusal(verdict)
                if verdict["status"] != "handoff_available" and not verdict["definitive"]:
                    # A caller in the wrong place, or one whose facts are unreadable, never ends another's attempt (pod#292).
                    raise refusal(verdict)
                # Only the operation can terminate a definitely lost attempt.
                abort(pending, "pending caller handle stale or gone", state)
            elif verdict["status"] != "handoff_available":
                if verdict["definitive"]:
                    abort(pending, verdict["condition"], state)
                raise refusal(verdict)
        if verdict["status"] != "handoff_available":
            raise refusal(verdict, "owner_handoff_refused")
        if generic and capability is None:
            recorded = ((pending or {}).get("resolution") if (pending or {}).get("state") == "aborted" else (pending or {}).get("decision"))
            if not isinstance(recorded, dict) or "decision_binding" not in recorded or not same_decision_input(requested_value, recorded):
                recorded = None
            authorize(recorded, completion=recorded is not None and verdict["condition"] == "resume")
        if capability is not None:
            capability.completion_only = verdict["condition"] == "resume"
        if pending and pending["state"] == "pending" and pending["decision"] != value:
            raise refusal({**verdict, "message": "The pending handoff requires its original exact confirmation."}, "handoff_decision_mismatch")
        record = state["checkpoint"].get("continuity") or {"binding": None, "history": []}
        if pending is None:
            pending = {"provenance": "owner_handoff", "state": "pending", "from_runtime": value["recorded_runtime"],
                       "to_runtime": value["current_runtime"], "at": capability.commit_clock() if capability else datetime.now(timezone.utc).isoformat(),
                       "verified": verdict["verified"], "decision": dict(value), "abort_reason": None, "completion": None}
            record["history"].append(pending)
            state["checkpoint"]["continuity"] = record
            _write(path, state)
        elif pending["state"] == "aborted":
            pending.update(state="pending", decision=dict(value), verified=verdict["verified"],
                           from_runtime=value["recorded_runtime"], to_runtime=value["current_runtime"],
                           at=capability.commit_clock() if capability else datetime.now(timezone.utc).isoformat())
            _write(path, state)
        failed = None
        if verdict["condition"] == "fresh":
            try:
                receipt = mutate_command(["orchestration", "run-use", "--id", value["run"], *FULL_JSON], accept_exit=(0, 1))
                if receipt.get("error") or receipt.get("exit") != 0:
                    failed = "run-use refused"
            except PodError:
                failed = "run-use outcome unknown"
            if failed:
                try:
                    after = current_run()
                except PodError:
                    after = None
                old = {"id": value["run"], "coordinator_handle": value["from_owner"],
                       "consumer_generation": value["recorded_generation"]}
                if after and after["runtime"] == value["current_runtime"] and after["run"] is None and verdict.get("unlinked"):
                    # An unlinked caller's authoritative readback is the Run itself (pod#292).
                    shown = _shown_again(value["run"])
                    if shown and shown["runtime"] == value["current_runtime"] and shown["coordinator_known"] and {
                            "id": shown["run"], "coordinator_handle": shown["coordinator_handle"],
                            "consumer_generation": shown["consumer_generation"]} == old:
                        abort(pending, failed, state)
                elif after and after["runtime"] == value["current_runtime"] and after["run"] == old:
                    abort(pending, failed, state)
                raise PodError("owner_handoff_refused", failed + "; re-read status and rerun the same handoff confirmation; no work was changed",
                               {"handoff_state": pending["state"]})
        final = detect(project, objective, state, port=port)
        if final["status"] != "handoff_available" or final["condition"] != "resume":
            if final["definitive"]:
                abort(pending, final["condition"], state)
            raise refusal(final)
        if any(final["facts"][key] != value[key] for key in FACTS):
            raise refusal({**final, "message": "Handoff facts changed after the rebind; the record stays pending."})
        run = final["verified"]
        state["owner"] = owner
        for row in state["admissions"].values():
            row.update(owner=owner, runtime=value["current_runtime"])
        for ref in state["checkpoint"].get("native_refs", []):
            ref["runtime"] = value["current_runtime"]
        record["binding"] = {"run": value["run"], "coordinator": owner, "generation": run["generation"]}
        pending.update(state="done", completion=run)
        if capability is not None:
            capability.completion_only = True
        _write(path, state)
        return {"status": "handed_off", "objective": objective, "owner_handoff": pending}
